#!/bin/sh
# harvest.sh -- PostToolUse hook for the gitian-kb plugin: passively harvest MCP traffic into the
# shared nudge-layer state file (see state.py / lib-state.sh for the state contract).
#
# Registered with matcher "mcp__.*gitian.*|ReadMcpResourceTool" so it fires on every gitian tool
# call and every resource read, but that matcher is only a coarse pre-filter -- harvest.py
# re-checks tool_name/tool_input itself and is silent on anything that isn't actually a gitian
# call. All JSON handling happens in one python3 invocation; this script never reads stdin itself
# -- it runs harvest.py as a child process (not `exec`, so this script's own `exit 0` below still
# runs after) with stdin passed straight through, unread and unmodified.
#
# Fail-open: harvest.py never raises past its own top-level guard and always exits 0. This
# wrapper only needs its own guard around resolving its directory (to find harvest.py); if that
# fails, or harvest.py is missing, or python3 itself is missing, it still exits 0 silently.
set -u

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
[ -f "$d/harvest.py" ] || exit 0

python3 "$d/harvest.py" 2>/dev/null
exit 0
