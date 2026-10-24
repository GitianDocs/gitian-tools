#!/bin/sh
# prompt-checkpoint.sh -- UserPromptSubmit hook for the gitian-kb plugin: the checkpoint that costs
# no extra turn. On the user's next prompt it adds, as context on that very message, a one-line
# hint when the prompt reads like a standing rule and a short list of activity no KB write covers
# yet once it has gone unwritten long enough (see prompt_checkpoint.py and ledger.py). Prints
# nothing when the session is compliant -- the agent has briefed the scribe by the time the user
# types again.
#
# All JSON handling happens in one python3 invocation; this script never reads stdin itself -- it
# runs prompt_checkpoint.py as a child process (not `exec`, so this script's own `exit 0` below
# still runs after) with stdin passed straight through, unread and unmodified (mirrors harvest.sh).
#
# Fail-open: prompt_checkpoint.py never raises past its own top-level guard and always exits 0.
# This wrapper only needs its own guard around resolving its directory (to find the script); if
# that fails, or the script or python3 itself is missing, it still exits 0 silently.
set -u

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
[ -f "$d/prompt_checkpoint.py" ] || exit 0

python3 "$d/prompt_checkpoint.py" 2>/dev/null
exit 0
