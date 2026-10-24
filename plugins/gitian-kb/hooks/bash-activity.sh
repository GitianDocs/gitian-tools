#!/bin/sh
# bash-activity.sh -- PostToolUse hook for the gitian-kb plugin: record what a Bash command did
# (a commit, a pull request opened or set to merge, a release, a file edited by sed/tee/a redirect)
# in the session's activity ledger, so the checkpoint nudges can say what is unwritten (see
# bash_activity.py and ledger.py). It replaces the old once-per-session commit nudge, and unlike it
# is RECORD-ONLY: it prints nothing, ever -- a nudge right after the event lands before a compliant
# agent could act on it.
#
# Registered with matcher "Bash", but that matcher is only a coarse pre-filter -- bash_activity.py
# re-checks tool_name/tool_input itself and returns before it touches any state for a command that
# did none of those things (nearly all of them). All JSON handling happens in one python3
# invocation; this script never reads stdin itself -- it runs bash_activity.py as a child process
# (not `exec`, so this script's own `exit 0` below still runs after) with stdin passed straight
# through, unread and unmodified (mirrors harvest.sh).
#
# Fail-open: bash_activity.py never raises past its own top-level guard and always exits 0. This
# wrapper only needs its own guard around resolving its directory (to find the script); if that
# fails, or the script or python3 itself is missing, it still exits 0 silently.
set -u

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
[ -f "$d/bash_activity.py" ] || exit 0

python3 "$d/bash_activity.py" 2>/dev/null
exit 0
