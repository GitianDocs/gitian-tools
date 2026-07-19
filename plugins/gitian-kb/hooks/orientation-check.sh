#!/bin/sh
# orientation-check.sh -- PreToolUse hook for the gitian-kb plugin's nudge layer.
#
# Registered (see hooks.json, owned by T12) on matcher "Edit|Write|NotebookEdit". Guards against
# a session's first file mutation happening before any gitian KB read: if this session has zero
# gitianReads recorded and the "orientation" flag hasn't fired yet, deny-once with an advisory
# reason pointing at file_intents/search/neighbors -- in-flight plans elsewhere may already claim
# the paths about to be touched. A session that has done even one gitian read never sees this,
# from the very first mutation onward.
#
# Fail-open on every path: bad/garbage/empty stdin, a missing session_id, a missing lib-state.sh,
# or any state-substrate failure (corrupt/unwritable state file, missing python3) all fall through
# to silence -- a denial is only ever emitted when gks_flag_once itself printed "fire".
set -u

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || exit 0
[ -f "$d/lib-state.sh" ] || exit 0
# shellcheck disable=SC1091 # dynamic script-dir path -- shellcheck can't resolve it statically.
. "$d/lib-state.sh"

hook_input="$(cat 2>/dev/null || true)"

# All JSON parsing delegates to python3 (stdlib only); each extraction is independently
# fail-open -- unparsable/missing input yields an empty string, never a traceback.
sid="$(printf '%s' "$hook_input" | python3 -c '
import json, sys
try:
    obj = json.load(sys.stdin)
    v = obj.get("session_id") if isinstance(obj, dict) else None
    if isinstance(v, str) and v:
        sys.stdout.write(v)
except Exception:
    pass
' 2>/dev/null)"
[ -n "$sid" ] || exit 0

cwd="$(printf '%s' "$hook_input" | python3 -c '
import json, sys
try:
    obj = json.load(sys.stdin)
    v = obj.get("cwd") if isinstance(obj, dict) else None
    if isinstance(v, str) and v:
        sys.stdout.write(v)
except Exception:
    pass
' 2>/dev/null)"
[ -n "$cwd" ] || cwd="."

reads="$(gks_get "sessions.$sid.gitianReads")"
case "$reads" in
  "") reads=0 ;;
  *[!0-9]*) reads=1 ;; # not a clean non-negative integer -- fail open, treat as already read
esac

if [ "$reads" = "0" ]; then
  fired="$(gks_flag_once "$sid" orientation)"
  if [ "$fired" = "fire" ]; then
    # --- repo: same owner/name normalization as session-context.sh -----------------------------
    remote_url="$(git -C "$cwd" remote get-url origin 2>/dev/null || true)"
    repo=""
    if [ -n "$remote_url" ]; then
      candidate="$(printf '%s' "$remote_url" |
        sed -E 's#^git@([^:]+):#https://\1/#' |
        sed -E 's#\.git$##' |
        sed -E 's#^[a-z]+://[^/]+/##')"
      # Only trust it if it reduced to a clean single owner/name pair -- same guard as
      # session-context.sh (subgroups/malformed URLs aren't safe to hand the model).
      case "$candidate" in
        */*/*) ;; # more than one slash -- reject
        */*) repo="$candidate" ;;
      esac
    fi

    if [ -n "$repo" ]; then
      intents_clause="\`file_intents\` on ${repo}"
    else
      intents_clause="\`file_intents\`"
    fi

    reason="gitian-kb orientation: this is your first file mutation this session with zero gitian KB reads so far. In-flight plans elsewhere may already claim these paths -- consider ${intents_clause} plus \`search\`/\`neighbors\` for the task topic before continuing. This is advisory: re-sending the identical call will pass through untouched. Fires once per session."

    # JSON-escape the dynamic reason before interpolating into the literal template below.
    reason_escaped="$(printf '%s' "$reason" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$reason_escaped"
    exit 0
  fi
fi

gks_incr "$sid" edits
exit 0
