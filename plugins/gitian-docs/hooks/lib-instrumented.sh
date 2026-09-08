#!/bin/sh
# lib-instrumented.sh -- shared "is this repo gitian-instrumented?" probe for the gitian-docs
# plugin's hooks.
#
# Sourced (never executed) from a hook script living in this same hooks/ directory, via the
# script-dir pattern:
#   d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd); . "$d/lib-instrumented.sh"
# Sourcing with `.` does not change $0 -- it stays the calling hook's own path -- so a caller
# resolves this file the same way for every hook in this directory. Never rely on
# CLAUDE_PLUGIN_ROOT here: it is set for the hook command line, not for anything this file
# needs, and the script-dir pattern keeps the library usable from tests too.
#
# Every function below is safe under `set -u`, always returns 0 (exit-status-neutral, so a
# caller can invoke it without guarding), and never writes to stderr. Sourcing has no side
# effects -- nothing is created or probed until a function is called.
#
# Definitions:
#   gd_state_dir                       -- print (and lazily create) the plugin's state directory
#   gd_session_id HOOK_JSON            -- print the session id for the current hook invocation
#   gd_instrumented PROJECT_DIR SESSID -- print `gitian-dir`, `annotations`, or nothing
#   gd_prune                           -- drop state files older than 7 days

# gd_state_dir -- where per-session markers and probe caches live. GITIAN_DOCS_STATE_DIR wins
# (tests point it at a tempdir); otherwise it sits beside the other Claude Code state under
# ~/.claude/gitian-docs. Created lazily and fail-open: an unwritable path just means the
# callers' marker/cache writes below quietly no-op.
gd_state_dir() {
  _gd_sd="${GITIAN_DOCS_STATE_DIR:-${CLAUDE_CONFIG_DIR:-${HOME:-.}/.claude}/gitian-docs}"
  mkdir -p "$_gd_sd" 2>/dev/null || true
  printf '%s\n' "$_gd_sd"
  return 0
}

# gd_session_id HOOK_JSON -- the id this session's marker/cache files are keyed on.
# Claude Code passes `session_id` in the hook's stdin JSON; it does NOT export a
# CLAUDE_SESSION_ID variable, so parsing stdin is the only reliable source. Falls back to
# CLAUDE_CODE_SESSION_ID (exported for some surfaces) and finally to $PPID, which at least
# scopes markers to one Claude Code process. The result is sanitized down to path-safe
# characters so it can be concatenated into a filename.
gd_session_id() {
  _gd_json="${1:-}"
  _gd_sid="$(printf '%s' "$_gd_json" | grep -o '"session_id" *: *"[^"]*"' | head -n 1 |
    sed 's/.*"session_id" *: *"\([^"]*\)".*/\1/')"
  [ -n "$_gd_sid" ] || _gd_sid="${CLAUDE_CODE_SESSION_ID:-}"
  [ -n "$_gd_sid" ] || _gd_sid="$PPID"
  printf '%s' "$_gd_sid" | tr -c 'A-Za-z0-9_.-' '-'
  printf '\n'
  return 0
}

# gd_instrumented PROJECT_DIR SESSION_ID -- is PROJECT_DIR a repo that uses gitian?
# Prints `gitian-dir` when a .gitian/ config directory exists, `annotations` when tracked
# non-markdown source carries an `@gitian:` tag, and nothing otherwise. Markdown/text files are
# excluded deliberately: prose that merely *documents* the syntax (a README, an adoption prompt,
# this plugin's own skill) must not make a repo look instrumented.
#
# The verdict is cached per session in <state>/probe-<SESSION_ID> as "<verdict>\t<PROJECT_DIR>"
# (an empty verdict is cached too, as just "\t<PROJECT_DIR>") so a PostToolUse hook firing on
# every edit costs one file read rather than one repo-wide grep. The recorded project dir is
# part of the cache: a session that moves to a different project recomputes and overwrites.
gd_instrumented() {
  _gd_proj="${1:-}"
  _gd_isid="${2:-}"
  [ -n "$_gd_proj" ] || return 0

  _gd_cache=""
  if [ -n "$_gd_isid" ]; then
    _gd_cache="$(gd_state_dir)/probe-$_gd_isid"
    if [ -f "$_gd_cache" ]; then
      _gd_line="$(head -n 1 "$_gd_cache" 2>/dev/null || true)"
      if [ "$(printf '%s' "$_gd_line" | cut -f 2-)" = "$_gd_proj" ]; then
        _gd_hitv="$(printf '%s' "$_gd_line" | cut -f 1)"
        [ -n "$_gd_hitv" ] && printf '%s\n' "$_gd_hitv"
        return 0
      fi
    fi
  fi

  _gd_verdict=""
  if [ -d "$_gd_proj/.gitian" ]; then
    _gd_verdict="gitian-dir"
  else
    _gd_hit="$(git -C "$_gd_proj" grep -l -I -e '@gitian:' -- . \
      ':(exclude)node_modules' ':(exclude)*.md' ':(exclude)*.mdx' ':(exclude)*.txt' \
      2>/dev/null | head -n 1)"
    if [ -z "$_gd_hit" ] && ! git -C "$_gd_proj" rev-parse --git-dir >/dev/null 2>&1; then
      # Not a git repo at all -- git grep can't answer, so walk the tree directly.
      _gd_hit="$(grep -rIl --exclude-dir=node_modules --exclude-dir=.git \
        --exclude='*.md' --exclude='*.mdx' --exclude='*.txt' \
        -e '@gitian:' "$_gd_proj" 2>/dev/null | head -n 1)"
    fi
    [ -n "$_gd_hit" ] && _gd_verdict="annotations"
  fi

  if [ -n "$_gd_cache" ]; then
    printf '%s\t%s\n' "$_gd_verdict" "$_gd_proj" >"$_gd_cache" 2>/dev/null || true
  fi

  [ -n "$_gd_verdict" ] && printf '%s\n' "$_gd_verdict"
  return 0
}

# gd_prune -- state files are per-session and never explicitly cleaned up, so drop anything
# older than a week on each hook run. Fail-open and silent.
gd_prune() {
  _gd_pd="$(gd_state_dir)"
  [ -n "$_gd_pd" ] && find "$_gd_pd" -type f -mtime +7 -delete 2>/dev/null
  return 0
}
