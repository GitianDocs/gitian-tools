#!/bin/sh
# brief-watch.sh -- PostToolUse hook for the gitian-kb plugin: record that the primary briefed
# kb-scribe (a dispatch, or a SendMessage to a scribe it already knows), so the checkpoint nudges
# measure activity against that brief instead of nagging about work already handed over (see
# brief_watch.py and ledger.py).
#
# Registered with matcher "Agent|Task|SendMessage", but that matcher is only a coarse pre-filter --
# brief_watch.py re-checks tool_name/tool_input itself and does nothing for any dispatch or message
# that is not a kb-scribe brief. It is SILENT ALWAYS. All JSON handling happens in one python3
# invocation; this script never reads stdin itself -- it runs brief_watch.py as a child process (not
# `exec`, so this script's own `exit 0` below still runs after) with stdin passed straight through,
# unread and unmodified (mirrors harvest.sh).
#
# Fail-open: brief_watch.py never raises past its own top-level guard and always exits 0. This
# wrapper only needs its own guard around resolving its directory (to find the script); if that
# fails, or the script or python3 itself is missing, it still exits 0 silently.
set -u

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
[ -f "$d/brief_watch.py" ] || exit 0

python3 "$d/brief_watch.py" 2>/dev/null
exit 0
