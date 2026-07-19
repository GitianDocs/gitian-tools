#!/bin/sh
# lib-state.sh -- POSIX-sh wrappers around state.py, the gitian-kb nudge layer's state substrate.
#
# Sourced (never executed) from a hook script living in this same hooks/ directory, via the
# script-dir pattern:
#   d=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd); . "$d/lib-state.sh"
# Sourcing a file with `.` does not change $0 -- it stays the calling hook script's own path --
# so this file resolves state.py the same way the caller resolved lib-state.sh: dirname of $0.
# That only works because every real caller IS a script file in this directory (as invoked by
# hooks.json, e.g. `sh "${CLAUDE_PLUGIN_ROOT}/hooks/foo.sh"`); it is not safe to source this from
# a script living elsewhere. Never rely on CLAUDE_PLUGIN_ROOT here (see hook idiom notes).
#
# Sourcing this file has no side effects beyond defining _gks_state_py/functions below -- it
# touches no state, prints nothing, and runs no state.py subcommand until a wrapper is called.
#
# Every wrapper is fail-open: state.py itself never exits non-zero or prints on error, and each
# wrapper additionally guards missing args / a missing state.py so a caller can invoke these
# without checking anything and always keep going.
# "CDPATH= cd" below is a deliberate prefix assignment (clear CDPATH only for this `cd`, per the
# contract's script-dir pattern), not a mistyped var assignment.
# shellcheck disable=SC1007
_gks_lib_dir=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || _gks_lib_dir=""
_gks_state_py=""
if [ -n "$_gks_lib_dir" ] && [ -f "$_gks_lib_dir/state.py" ]; then
  _gks_state_py="$_gks_lib_dir/state.py"
fi

# gks_state_file -- print the resolved state file path (mirrors state.py's own resolution).
gks_state_file() {
  printf '%s\n' "${GITIAN_KB_STATE_FILE:-$HOME/.claude/gitian-kb/state.json}"
}

# gks_now -- current UTC time as ISO-8601 (matches the timestamps state.py stores).
gks_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

# gks_get DOTPATH -- print the raw value at DOTPATH, or nothing if absent/on any error.
gks_get() {
  _gks_dotpath="${1:-}"
  [ -n "$_gks_dotpath" ] && [ -n "$_gks_state_py" ] || return 0
  python3 "$_gks_state_py" get "$_gks_dotpath" 2>/dev/null
  return 0
}

# gks_merge -- deep-merge a JSON document (read from stdin) into state. Silent either way.
gks_merge() {
  [ -n "$_gks_state_py" ] || return 0
  python3 "$_gks_state_py" merge 2>/dev/null
  return 0
}

# gks_incr SID COUNTER [DELTA] -- bump sessions.SID.COUNTER by DELTA (default 1). Silent.
gks_incr() {
  _gks_sid="${1:-}"
  _gks_counter="${2:-}"
  _gks_delta="${3:-}"
  [ -n "$_gks_sid" ] && [ -n "$_gks_counter" ] && [ -n "$_gks_state_py" ] || return 0
  if [ -n "$_gks_delta" ]; then
    python3 "$_gks_state_py" incr "$_gks_sid" "$_gks_counter" "$_gks_delta" 2>/dev/null
  else
    python3 "$_gks_state_py" incr "$_gks_sid" "$_gks_counter" 2>/dev/null
  fi
  return 0
}

# gks_flag_once SID FLAG -- print "fire" iff this call is the one that set the flag; silent
# (no output) if the flag was already set, or on any error.
gks_flag_once() {
  _gks_sid="${1:-}"
  _gks_flag="${2:-}"
  [ -n "$_gks_sid" ] && [ -n "$_gks_flag" ] && [ -n "$_gks_state_py" ] || return 0
  python3 "$_gks_state_py" flag-once "$_gks_sid" "$_gks_flag" 2>/dev/null
  return 0
}

# gks_bump_epoch SID -- advance the session's epoch, clearing flags/lintHashes/mintPrompted and
# zeroing counters (lastSeenVocabRev survives). Silent.
gks_bump_epoch() {
  _gks_sid="${1:-}"
  [ -n "$_gks_sid" ] && [ -n "$_gks_state_py" ] || return 0
  python3 "$_gks_state_py" bump-epoch "$_gks_sid" 2>/dev/null
  return 0
}

# gks_status -- human-readable summary of the whole state file; never errors on empty state.
gks_status() {
  [ -n "$_gks_state_py" ] || return 0
  python3 "$_gks_state_py" status 2>/dev/null
  return 0
}
