#!/usr/bin/env python3
"""publish_reminder.py -- Stop hook for the gitian-kb plugin's nudge layer.

Invoked by publish-reminder.sh (stdin passed straight through, unread by the wrapper) on every
Stop event. Streams the turn's transcript looking for a session that has edited files or made a
commit without ever publishing to the gitian KB, and -- once per session -- blocks with an
advisory reminder to record the work if it clears the gitian-kb skill's meaningful-event bar.

Fail-open, always: the loop guard (stop_hook_active) is checked first and unconditionally; a
missing/unreadable transcript, corrupt state, or any other error anywhere below is swallowed by
the top-level guard in `main()`'s caller and the process always exits 0 having printed nothing.

Run directly: python3 publish_reminder.py < stop_envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_publish_reminder.py (drives it via publish-reminder.sh,
end to end).
"""

import json
import sys

# publish_reminder.py always runs as a script file (never `python3 -c ...`), so Python has already
# put its own directory at sys.path[0] -- `import state`/`import commit_nudge` below resolve their
# sibling modules directly, no path manipulation needed (matches harvest.py's own house style).
import commit_nudge as commit_nudge_mod
import state as state_mod

EDIT_TOOL_NAMES = ("Edit", "Write", "NotebookEdit")
PUBLISH_MARKERS = ("publish_doc", "publish_memory", "publish_entry", "publish_topic", "append_entry")
FLAG_NAME = "publish_reminder"

# Pinned decision 4: publishes == 0 AND (edits >= EDIT_THRESHOLD OR commits >= COMMIT_THRESHOLD).
EDIT_THRESHOLD = 3
COMMIT_THRESHOLD = 1


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


def _command_has_git_commit(command):
    """Segment-aware git-commit detection -- delegated straight to commit_nudge.py's own
    `_is_git_commit_segment` (segment split via its `_SEGMENT_SPLIT_RE`: |, ;, &&, or a newline),
    so both hooks judge the exact same Bash command the exact same way through ONE implementation
    rather than two that could silently drift. `_is_git_commit_segment` shlex-tokenizes each
    segment (so a quoted phrase -- e.g. `gh pr create --body "after review, run git commit
    again"` -- stays ONE token, never separate `git`/`commit` words) and anchors the match to the
    segment's command head, which is what keeps a mere quoted MENTION of "git commit" inside
    another command's argument from ever being misread as a commit action -- the naive whitespace
    token scan this used to duplicate here could not tell the two apart."""
    if not isinstance(command, str) or not command.strip():
        return False
    return any(
        commit_nudge_mod._is_git_commit_segment(segment)
        for segment in commit_nudge_mod._SEGMENT_SPLIT_RE.split(command)
    )


def _is_publish_tool(tool_name):
    return isinstance(tool_name, str) and "gitian" in tool_name and any(
        marker in tool_name for marker in PUBLISH_MARKERS
    )


def _tool_use_blocks(line_obj):
    """Yield tool_use content blocks from one parsed transcript line, walking defensively --
    anything not shaped like {"type":"assistant","message":{"content":[...]}} yields nothing."""
    if not isinstance(line_obj, dict) or line_obj.get("type") != "assistant":
        return
    message = line_obj.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            yield block


def _scan_transcript(path):
    """Stream the transcript JSONL line by line -- never slurp the whole file, transcripts reach
    tens of MB. Each line is its own json.loads; unparsable lines are skipped, not fatal. Raises on
    a missing/unreadable file -- the caller treats that as silent (fail-open)."""
    edits = 0
    commits = 0
    publishes = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                line_obj = json.loads(line)
            except Exception:
                continue
            for block in _tool_use_blocks(line_obj):
                name = block.get("name")
                tool_input = block.get("input")
                tool_input = tool_input if isinstance(tool_input, dict) else {}
                if isinstance(name, str) and name in EDIT_TOOL_NAMES:
                    edits += 1
                elif name == "Bash" and _command_has_git_commit(tool_input.get("command")):
                    commits += 1
                if _is_publish_tool(name):
                    publishes += 1
    return edits, commits, publishes


def _flag_already_set(state, sid, flag):
    """Peek sessions.<sid>.flags.<flag> WITHOUT consuming it -- fully defensive, never raises,
    so an oddly-shaped (corrupt) session/flags value just reads as "not set" rather than crashing
    the peek itself."""
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        return False
    session = sessions.get(sid)
    if not isinstance(session, dict):
        return False
    flags = session.get("flags")
    if not isinstance(flags, dict):
        return False
    return flags.get(flag) is True


def _touch_session(state, sid):
    sessions = state.setdefault("sessions", {})
    session = sessions.get(sid)
    if not isinstance(session, dict):
        session = {}
        sessions[sid] = session
    state_mod.ensure_session_shape(session)
    return session


def _flag_once(sid, flag):
    """In-process equivalent of state.py's `flag-once` CLI subcommand (see cmd_flag_once), used
    directly via the importable primitives rather than shelling out (matches harvest.py's own
    house style for sibling hook glue). Returns True iff THIS call is the one that set the flag."""
    path = state_mod.state_path()

    def mutate():
        state = state_mod.load(path)
        session = _touch_session(state, sid)
        flags = session.setdefault("flags", {})
        if flags.get(flag) is True:
            return False
        flags[flag] = True
        session["updatedAt"] = state_mod.now_iso()
        state_mod.save(path, state_mod.finalize(state))
        return True

    return state_mod.with_lock(path, mutate)


def _build_reason(edits, commits):
    parts = []
    if edits:
        parts.append("%d file edit%s" % (edits, "" if edits == 1 else "s"))
    if commits:
        parts.append("a commit" if commits == 1 else "%d commits" % commits)
    activity = " and ".join(parts) if parts else "activity"
    return (
        "%s this session with nothing published to the gitian KB. If any of it clears the "
        "meaningful-event bar (see the gitian-kb skill's trigger table), record it now -- "
        "append_entry for the journal, or update the governing doc. If nothing qualifies, finish "
        "normally: this reminder fires once per session." % activity
    )


def main():
    payload = _parse_stdin()

    # Loop guard -- ALWAYS first, unconditionally.
    if payload.get("stop_hook_active") is True:
        return

    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return

    # Check WITHOUT consuming: below-threshold turns must leave the flag unset so a later
    # turn-end that crosses the bar can still fire it.
    if _flag_already_set(state_mod.load(), sid, FLAG_NAME):
        return

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return

    try:
        edits, commits, publishes = _scan_transcript(transcript_path)
    except Exception:
        return  # missing transcript, unreadable file, any parse-adjacent error -> silent

    if publishes != 0:
        return
    if edits < EDIT_THRESHOLD and commits < COMMIT_THRESHOLD:
        return  # below threshold -- flag must NOT be consumed

    if not _flag_once(sid, FLAG_NAME):
        return  # already fired (race with another turn) -- stay silent

    sys.stdout.write(json.dumps({"decision": "block", "reason": _build_reason(edits, commits)}))
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit, never a partial nudge
    sys.exit(0)
