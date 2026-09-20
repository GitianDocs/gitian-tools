#!/usr/bin/env python3
"""routing_guard.py -- PreToolUse routing-precondition guard for the gitian-kb plugin.

Invoked as a single python3 process (stdin passed straight through, unread by routing-guard.sh) by
routing-guard.sh, itself registered as a PreToolUse hook matching
"mcp__.*(publish_doc|publish_entry|append_entry)" -- the three gitian write tools whose target KB
is decided by ROUTING rather than by the caller.

The incident this exists for: an agent told to write to an org's team KB called append_entry with
neither `kb` nor `repo`. Routing needs `repo` (a doc/entry whose `repo` owner names one of the
caller's orgs lands in `<org>/team`, reported as `landed_in`), so with both absent the only possible
destination is `home` -- the team journal forked silently into a personal KB. Prompt-side wording is
the primary fix; this hook is the backstop that makes the silent case impossible to miss. Memories
never route, so they are never guarded; neither are the patch/retract/topic tools.

DENY (the one and only condition, all three parts required):
  1. tool_name contains "gitian" AND one of publish_doc / publish_entry / append_entry (the
     hooks.json matcher is a coarse pre-filter; this guard is the real gate), and
  2. tool_input carries no non-blank `kb` and no non-blank `repo`, and
  3. the payload's cwd is inside a git repo whose `origin` remote reduces to a clean GitHub-style
     `owner/name` pair -- one `git remote get-url origin` call, the same normalization
     session-context.sh and orientation-check.sh use, so the `repo` this reason suggests is
     byte-identical to the one those surfaces already told the model to use.
Anything else is allowed, silently.

Deliberately STATELESS and deterministic -- unlike the nudge-layer hooks (see state.py), this is a
precondition on the call rather than advice about the session, so it must not go quiet after the
first firing: an identical re-send still cannot route. The remedy travels in the reason itself
(set `repo`, or pass `kb` explicitly -- `"home"` for genuinely personal work), so a caller is never
stuck. It also fires inside a subagent (PreToolUse hooks do), which is why the reason speaks to an
agent holding a brief as well as to one holding a cwd.

Fail OPEN is the contract, everywhere: unparsable stdin, a malformed `tool_input`, a missing
session/cwd, no git binary, not a repo, no `origin`, a remote that doesn't reduce to owner/name
(GitLab subgroups included), a git call that exceeds GIT_TIMEOUT_SECONDS, or any other exception --
all of it degrades to silence and exit 0. A guard that blocked a publish because a subprocess
hiccuped would be worse than the routing mistake it prevents. One git invocation, short timeout,
no network.

Run directly: python3 routing_guard.py < envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_routing_guard.py (drives it via routing-guard.sh).
"""

import json
import re
import subprocess
import sys

# Only the three write tools whose destination routing decides. `publish_memory` is absent by design
# (memories never route, so a missing `repo` doesn't change where one lands), and so are the
# patch/retract/topic tools.
ROUTED_WRITE_MARKERS = ("publish_doc", "publish_entry", "append_entry")

GIT_TIMEOUT_SECONDS = 2

# user@host:owner/name -> https://host/owner/name, so one strip handles every form afterwards.
# Deliberately broader than `git@`: a deploy key or a self-hosted forge often uses another user.
_SCP_FORM_RE = re.compile(r"^[^/@]+@([^:/]+):")
_SCHEME_HOST_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+/")


def _parse_stdin():
    """Read the whole hook-input envelope. Unparsable/non-object stdin -> {}."""
    try:
        payload = json.loads(sys.stdin.read())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _blank(value):
    """A field counts as absent when it's missing, null, or an all-whitespace string."""
    if value is None:
        return True
    if not isinstance(value, str):
        return False  # a non-string `kb`/`repo` is the server's problem, not this hook's
    return value.strip() == ""


def normalize_remote(remote_url):
    """A git remote -> "owner/name", or "" when it doesn't reduce cleanly.

    Mirrors the sed chain in session-context.sh / orientation-check.sh: rewrite the scp-style
    `user@host:` prefix to a URL, drop scheme+host, drop trailing slashes and a trailing `.git`,
    then insist on exactly one slash with both halves non-empty. Anything else (a GitLab subgroup
    path, a malformed URL, a bare host) is not safe to hand back as `repo`, so it reduces to "".
    """
    if not isinstance(remote_url, str):
        return ""
    candidate = remote_url.strip()
    if not candidate:
        return ""
    candidate = _SCP_FORM_RE.sub(r"https://\1/", candidate)
    candidate = _SCHEME_HOST_RE.sub("", candidate)
    candidate = candidate.rstrip("/")
    if candidate.endswith(".git"):
        candidate = candidate[: -len(".git")]
    parts = candidate.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return ""
    return candidate


def _origin_repo(cwd):
    """One `git remote get-url origin` in cwd, normalized. "" on any failure (fail open)."""
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except Exception:
        return ""  # no git binary, timeout, unreadable cwd -- all fail open
    if proc.returncode != 0:
        return ""  # not a repo, or no `origin`
    return normalize_remote(proc.stdout)


def deny_reason(repo):
    return (
        "gitian-kb routing guard: this write names no `kb` and no `repo`, so it has nothing to "
        "route on and can only land in `home` -- in an org checkout that silently forks the "
        "team's KB. Set one of them and re-send:\n"
        '- `repo: "%s"` (this checkout\'s origin) -- a doc or journal entry whose repo owner is a '
        "gitian org routes to `<org>/team`; the response says where it went in `landed_in`.\n"
        '- or pass `kb` explicitly -- `"home"` for a personal note, `"<org>/team"` for team work. '
        "An explicit `kb` always wins and fails loudly rather than landing somewhere else.\n"
        "- If you are a subagent writing from a brief, pass the brief's `kb` and `repo` verbatim; "
        "if it gives neither, hand back rather than guess." % repo
    )


def evaluate(payload):
    """Return the deny reason string, or None to allow silently."""
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or "gitian" not in tool_name:
        return None
    if not any(marker in tool_name for marker in ROUTED_WRITE_MARKERS):
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None  # nothing to inspect -- fail open
    if not _blank(tool_input.get("kb")):
        return None  # an explicit kb always wins; nothing to warn about
    if not _blank(tool_input.get("repo")):
        return None  # routing has what it needs

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None  # no directory to probe -- fail open

    repo = _origin_repo(cwd)
    if not repo:
        return None  # no GitHub-style origin to suggest -- fail open

    return deny_reason(repo)


def main():
    reason = evaluate(_parse_stdin())
    if reason is None:
        return
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit
    sys.exit(0)
