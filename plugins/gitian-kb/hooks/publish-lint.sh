#!/bin/sh
# publish-lint.sh -- PreToolUse hook for the gitian-kb plugin: advisory publish lint over gitian
# publish/append/patch calls, batch_write's operations included (see publish_lint.py for the rule
# and state.py for the state contract). It never blocks: advice travels as additionalContext, with no permissionDecision.
#
# Registered with the publish/append/patch matcher publish_lint.py's docstring quotes -- that
# matcher is only a coarse pre-filter; publish_lint.py re-checks tool_name itself (must contain
# "gitian") and is silent on anything else. All JSON handling happens in one python3 invocation;
# this script never reads stdin itself -- it execs publish_lint.py directly so stdin passes
# straight through, unread and unmodified.
#
# Fail-open: publish_lint.py never raises past its own top-level guard and always exits 0. This
# wrapper only needs its own guard around resolving its directory (to find publish_lint.py); if
# that fails, or publish_lint.py is missing, or python3 itself is missing, it still exits 0 silently.
set -u

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
[ -f "$d/publish_lint.py" ] || exit 0

python3 "$d/publish_lint.py" 2>/dev/null
exit 0
