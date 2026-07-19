#!/bin/sh
# commit-nudge.sh -- PostToolUse hook for the gitian-kb plugin: an advisory, once-per-session
# nudge to journal a commit via append_entry when no journal append has landed in the last 2
# hours (see commit_nudge.py for the full guard chain and the damper).
#
# Registered with matcher "Bash", but that matcher is only a coarse pre-filter -- commit_nudge.py
# re-checks tool_name/tool_input itself and is silent on anything that isn't actually a commit
# action (a real `git ... commit`, or `gh pr merge`). All JSON handling happens in one python3
# invocation; this script never reads stdin itself -- it runs commit_nudge.py as a child process
# (not `exec`, so this script's own `exit 0` below still runs after) with stdin passed straight
# through, unread and unmodified.
#
# Fail-open: commit_nudge.py never raises past its own top-level guard and always exits 0. This
# wrapper only needs its own guard around resolving its directory (to find commit_nudge.py); if
# that fails, or commit_nudge.py is missing, or python3 itself is missing, it still exits 0
# silently.
set -u

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
[ -f "$d/commit_nudge.py" ] || exit 0

python3 "$d/commit_nudge.py" 2>/dev/null
exit 0
