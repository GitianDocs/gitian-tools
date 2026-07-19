#!/usr/bin/env python3
"""commit_nudge.py -- PostToolUse commit-journaling nudge for the gitian-kb plugin.

Invoked by commit-nudge.sh (itself a PostToolUse hook matching "Bash"). Fires, at most once per
session, an advisory nudge when a Bash call that actually performed a commit action (a real
`git ... commit` invocation, or `gh pr merge`) just succeeded and no journal append
(`append_entry` / `publish_entry`) has landed on any tracked server in the last two hours (the
commit-nudge damper, pinned decision 7). The nudge tells the model to consider `append_entry` if
the commit clears the gitian-kb skill's meaningful-event bar -- it is advisory, never a
rejection, and never fires twice in the same session (state.py's per-session flags substrate).

Guard order (silent no-op on any failure of any of these):
  1. tool_name must be "Bash" (the hooks.json matcher is only a coarse pre-filter, same
     convention as harvest.py's own internal re-check).
  2. tool_input.command must look like a real commit action -- see _is_commit_action().
  3. the raw tool_response text must not look like a failed commit -- see _looks_like_failure().
     A failed commit never touches state at all (no write, no flag consumed).
  4. session_id must be a non-empty string.
  5. damper: no tracked server's lastAppendAt may fall within the last two hours -- checked
     WITHOUT consuming the once-per-session flag, so a later commit (once the damper window has
     passed) still gets its own chance to fire.
  6. the once-per-session flag ("commit_nudge") must not already be set for this session.

Fail-open, silent-always on error: any exception anywhere is swallowed by the top-level guard
below and the process always exits 0 (matches state.py's / harvest.py's own fail-open contract).
The nudge text is built into a single string and written with one stdout call, so there is no
window in which a partial/malformed nudge could be emitted.

Run directly: python3 commit_nudge.py < envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_commit_nudge.py (drives it via commit-nudge.sh, end to end).
"""

import json
import re
import shlex
import sys
from datetime import datetime, timedelta, timezone

# commit_nudge.py always runs as a script file (never `python3 -c ...`), so Python has already
# put its own directory at sys.path[0] -- `import state` resolves state.py as a sibling module,
# same convention harvest.py documents and uses.
import state as state_mod

FLAG_NAME = "commit_nudge"
DAMPER_WINDOW = timedelta(hours=2)

# Case-insensitive: real git/gh output capitalizes some of these ("Aborting commit due to an
# empty commit message"), and matching case-insensitively is strictly safer than the literal
# lowercase forms -- it only ever suppresses more false nudges, never fewer.
FAILURE_MARKERS = ("nothing to commit", "error:", "fatal:", "aborting commit")

_SEGMENT_SPLIT_RE = re.compile(r"&&|\||;|\n")

# Leading `VAR=value` env-assignment words before the real command word, e.g. `FOO=bar git commit`.
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# git global options that consume the FOLLOWING token as a separate argument (as opposed to a
# bare flag or a self-contained `--opt=value` token). `-C <path>` and `-c <key=val>` are the only
# ones common enough in practice to special-case here.
_GIT_GLOBAL_OPTS_WITH_ARG = ("-C", "-c")


def _parse_stdin():
    """Read the whole hook-input envelope. Unparsable/non-object stdin -> {}."""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    return payload


def _response_text(tool_response):
    """JSON-render the tool_response payload so the failure-marker scan runs over one flat string
    regardless of whether the tool surfaced it as a plain string or a structured dict (e.g.
    {"stdout": ..., "stderr": ...})."""
    if isinstance(tool_response, str):
        return tool_response
    try:
        return json.dumps(tool_response)
    except Exception:
        return ""


def _segment_tokens(segment):
    """Shell-word-split a segment via shlex rather than a plain `.split()` -- shlex keeps a
    quoted phrase as a single token (e.g. `--body "run git commit"` yields one trailing token,
    not separate `git`/`commit` words), which is what keeps a quoted *mention* of those words in
    another command's argument from ever reaching the subcommand scan below. Returns `[]` for an
    unparsable segment (e.g. unbalanced quotes) so callers just see "no tokens" rather than a
    raised ValueError."""
    try:
        return shlex.split(segment)
    except ValueError:
        return []


def _command_head_index(tokens):
    """Index of the segment's real command word: the first token that isn't a leading
    `VAR=value` env-assignment (e.g. `FOO=bar git commit` -> head is `git`, index 1, not
    `FOO=bar`). None if there are no tokens or the segment is nothing but assignments."""
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN_RE.match(tokens[idx]):
        idx += 1
    return idx if idx < len(tokens) else None


def _is_git_commit_segment(segment):
    """A real `git ... commit` invocation. Detection is anchored to the segment's command HEAD
    (after skipping any leading env-assignment words) rather than scanning every token in the
    segment -- that anchor is what keeps a command that merely *mentions* the words "git" and
    "commit" as ordinary arguments (e.g. `echo please git commit now`, or `git add -A && echo
    next step is git commit`) from being misread as a commit action. Once the head is confirmed
    to be `git` (or `*/git`), git's own global options are skipped to reach the subcommand token:
    `-C <path>` and `-c <key=val>` each consume the option AND its following argument (so
    `git -C repo commit -m x` and `git -c user.name=x commit` still match); any other
    `-`-prefixed token is a flag with no separate argument. A plain non-flag token other than
    `commit` (e.g. `git log --grep commit`, where `log` is the subcommand) means `commit` is just
    an argument, not the subcommand, and must NOT match."""
    tokens = _segment_tokens(segment)
    head_idx = _command_head_index(tokens)
    if head_idx is None:
        return False
    head = tokens[head_idx]
    if head != "git" and not head.endswith("/git"):
        return False
    j = head_idx + 1
    while j < len(tokens) and tokens[j].startswith("-"):
        j += 2 if tokens[j] in _GIT_GLOBAL_OPTS_WITH_ARG else 1
    return j < len(tokens) and tokens[j] == "commit"


def _is_gh_pr_merge_segment(segment):
    """`gh pr merge`, anchored the same way as git: the segment's command head (after skipping
    leading env-assignments) must be `gh`/`*/gh`, immediately followed by the `pr merge`
    subcommand pair -- not merely a `gh ...` invocation whose arguments happen to contain the
    words "pr" and "merge" elsewhere (e.g. inside a quoted string)."""
    tokens = _segment_tokens(segment)
    head_idx = _command_head_index(tokens)
    if head_idx is None:
        return False
    head = tokens[head_idx]
    if head != "gh" and not head.endswith("/gh"):
        return False
    return (
        head_idx + 2 < len(tokens)
        and tokens[head_idx + 1] == "pr"
        and tokens[head_idx + 2] == "merge"
    )


def _is_commit_action(command):
    """A real commit action: `git ... commit` within some shell segment (the command is split on
    |, ;, &&, or a newline first, so a commit hiding after a chained or newline-separated compound
    command -- e.g. `git add -A\\ngit commit -m x` -- is still found), or `gh pr merge` as a
    segment's own head + subcommand."""
    if not isinstance(command, str) or not command.strip():
        return False
    segments = _SEGMENT_SPLIT_RE.split(command)
    return any(_is_git_commit_segment(seg) or _is_gh_pr_merge_segment(seg) for seg in segments)


def _looks_like_failure(resp_text):
    lowered = resp_text.lower()
    return any(marker in lowered for marker in FAILURE_MARKERS)


def _parse_iso(value):
    """Best-effort ISO-8601 parse (accepts 'Z' or explicit offsets); None on anything unparsable.
    Local copy of state.py's own private helper -- that module's docstring only lists load/save/
    with_lock/ensure_session_shape/etc. as its importable surface, not the underscore-prefixed
    _parse_iso, so this stays a small local duplicate rather than reaching into it."""
    if not isinstance(value, str) or not value:
        return None
    try:
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _recent_append_exists(state):
    """Damper (pinned decision 7): True iff ANY tracked server's lastAppendAt falls within the
    last two hours -- not scoped to the current server, matching the pinned wording verbatim
    ("state shows any append_entry within the last 2 hours")."""
    servers = state.get("servers")
    if not isinstance(servers, dict):
        return False
    now = datetime.now(timezone.utc)
    for server in servers.values():
        if not isinstance(server, dict):
            continue
        dt = _parse_iso(server.get("lastAppendAt"))
        if dt is not None and now - dt < DAMPER_WINDOW:
            return True
    return False


def _flag_once(path, sid):
    """Mirrors state.py's cmd_flag_once, built directly from the public load/save/with_lock/
    finalize/ensure_session_shape primitives rather than shelling out to the CLI -- the same
    direct-import convention harvest.py uses for its own state mutation. Returns True iff this
    call is the one that set the flag."""

    def mutate():
        state = state_mod.load(path)
        sessions = state.setdefault("sessions", {})
        session = sessions.get(sid)
        if not isinstance(session, dict):
            session = {}
            sessions[sid] = session
        state_mod.ensure_session_shape(session)
        flags = session.setdefault("flags", {})
        if flags.get(FLAG_NAME) is True:
            return False
        flags[FLAG_NAME] = True
        session["updatedAt"] = state_mod.now_iso()
        state_mod.save(path, state_mod.finalize(state))
        return True

    return state_mod.with_lock(path, mutate)


NUDGE_TEXT = (
    "gitian-kb: a commit just landed and no journal append has happened in the last 2 hours. "
    "If this clears the meaningful-event bar in the gitian-kb skill's trigger table, record it "
    "now with append_entry (or publish_entry for a new entry). Advisory only -- fires once per "
    "session."
)


def main():
    payload = _parse_stdin()
    if payload.get("tool_name") != "Bash":
        return
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if not _is_commit_action(tool_input.get("command")):
        return
    if _looks_like_failure(_response_text(payload.get("tool_response"))):
        return
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return

    path = state_mod.state_path()
    state = state_mod.load(path)
    if _recent_append_exists(state):
        return  # damper -- silent, and deliberately does NOT consume the once-flag

    if not _flag_once(path, sid):
        return

    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": NUDGE_TEXT,
                }
            }
        )
    )
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit
    sys.exit(0)
