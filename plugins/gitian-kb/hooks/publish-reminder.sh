#!/bin/sh
# publish-reminder.sh -- Stop hook for the gitian-kb plugin: the completion checkpoint. At a turn
# end it polls the session's watched pull requests for a merge (pr_poll.py) and blocks, once, with
# a list of the activity no KB write covers yet -- but only at a completion point (the session's
# first work, a merge, a release; ledger.py decides), because a block costs a whole extra turn.
# Everything else waits for the next prompt's context line (prompt-checkpoint.sh).
#
# All JSON/state handling happens in one python3 invocation; this script never reads stdin
# itself -- it runs publish_reminder.py as a child process (not `exec`, so this script's own
# `exit 0` below still runs after) with stdin passed straight through, unread and unmodified
# (mirrors harvest.sh).
#
# Fail-open: publish_reminder.py never raises past its own top-level guard and always exits 0.
# This wrapper only needs its own guard around resolving its directory (to find the script); if
# that fails, or the script or python3 itself is missing, it still exits 0 silently.
set -u

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
[ -f "$d/publish_reminder.py" ] || exit 0

python3 "$d/publish_reminder.py" 2>/dev/null
exit 0
