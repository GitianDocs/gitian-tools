#!/bin/sh
# routing-guard.sh -- PreToolUse hook for the gitian-kb plugin: routing-precondition guard over
# the three gitian write tools whose destination KB is decided by routing (see routing_guard.py
# for the rule and its fail-open contract).
#
# Registered with matcher "mcp__.*(publish_doc|publish_entry|append_entry)" -- that matcher is only
# a coarse pre-filter; routing_guard.py re-checks tool_name itself (must contain "gitian") and is
# silent on anything else. All JSON handling happens in one python3 invocation; this script never
# reads stdin itself -- it runs routing_guard.py directly so stdin passes straight through, unread
# and unmodified.
#
# Fail-open: routing_guard.py never raises past its own top-level guard and always exits 0. This
# wrapper only needs its own guard around resolving its directory (to find routing_guard.py); if
# that fails, or routing_guard.py is missing, or python3 itself is missing, it still exits 0
# silently.
set -u

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
[ -f "$d/routing_guard.py" ] || exit 0

python3 "$d/routing_guard.py" 2>/dev/null
exit 0
