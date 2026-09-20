#!/bin/sh
# orientation-check.sh -- PreToolUse hook for the gitian-kb plugin's nudge layer.
#
# Registered (see hooks.json, owned by T12) on matcher "Edit|Write|NotebookEdit". Guards against
# a session's first file mutation happening before any gitian KB read: if this session has zero
# gitianReads recorded and the "orientation" flag hasn't fired yet, deny-once with an advisory
# reason pointing at a kb-librarian dispatch (file_intents/search/neighbors) -- in-flight plans
# elsewhere may already claim the paths about to be touched. A session that has done even one
# gitian read never sees this, from the very first mutation onward -- and a BACKGROUND librarian's
# reads count, since harvest.py records a subagent's MCP traffic under the parent's session id.
#
# A write into Claude Code's EPHEMERAL state -- <config>/projects/ (agent memory, transcripts) and
# <config>/plans/, where <config> is $CLAUDE_CONFIG_DIR or ~/.claude -- is exempt entirely: no
# in-flight plan can claim such a path, so denying it taught agents that the nudge is noise. It
# neither fires the flag nor counts as an edit (the Stop publish-reminder counts edits, and a
# memory write is not repo work either), so the first REAL mutation still gets the nudge. The list
# is an allowlist on purpose: <config>/plugins, agents, skills and commands are AUTHORED source,
# and a session whose whole task is editing a plugin there must still be nudged. A relative target
# is resolved against the payload's cwd first; the match is lexical (no `..` resolution), which is
# fine for an advisory nudge.
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

# --- exempt: the target is Claude Code's own ephemeral state (memory, transcripts, plans) ------
target="$(printf '%s' "$hook_input" | python3 -c '
import json, sys
try:
    obj = json.load(sys.stdin)
    ti = obj.get("tool_input") if isinstance(obj, dict) else None
    ti = ti if isinstance(ti, dict) else {}
    for key in ("file_path", "notebook_path"):
        v = ti.get(key)
        if isinstance(v, str) and v:
            sys.stdout.write(v)
            break
except Exception:
    pass
' 2>/dev/null)"
if [ -n "$target" ]; then
  case "$target" in
    /*) ;;
    *) target="${cwd%/}/${target}" ;;
  esac
  config_dir="${CLAUDE_CONFIG_DIR:-${HOME:-}/.claude}"
  case "$config_dir" in
    /*)
      case "$target" in
        "${config_dir%/}"/projects/* | "${config_dir%/}"/plans/*) exit 0 ;;
      esac
      ;;
  esac
fi

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

    reason="gitian-kb orientation: this is your first file mutation this session with zero gitian KB reads so far. In-flight plans elsewhere may already claim these paths -- dispatch \`kb-librarian\` (background) for an orientation digest: ${intents_clause} plus \`search\`/\`neighbors\` for the task topic. This is advisory: re-sending the identical call will pass through untouched. Fires once per session."

    # JSON-escape the dynamic reason before interpolating into the literal template below.
    reason_escaped="$(printf '%s' "$reason" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
    printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}\n' "$reason_escaped"
    exit 0
  fi
fi

gks_incr "$sid" edits
exit 0
