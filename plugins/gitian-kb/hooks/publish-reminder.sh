#!/bin/sh
# publish-reminder.sh -- Stop hook for the gitian-kb plugin: nudge once per session when file
# edits or a commit happened with nothing published to the gitian KB (pinned decision 4).
#
# All JSON/transcript handling happens in one python3 invocation; this script never reads stdin
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
