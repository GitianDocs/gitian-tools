#!/bin/sh
# gitian-docs-nudge.sh -- PostToolUse hook for the gitian-docs plugin.
#
# Fires after Edit/Write/MultiEdit (see hooks/hooks.json). Must be fast and quiet: it exits 0
# with no output unless ALL of the following hold:
#   (a) the edited file has a code extension,
#   (b) the project is gitian-instrumented (.gitian/ exists at $CLAUDE_PROJECT_DIR),
#   (c) this session hasn't already been nudged (marker file).
# When all three hold, it emits a one-line PostToolUse additionalContext reminder pointing at
# the gitian-docs skill, then writes the marker so it only fires once per session.
set -u

input="$(cat)"

file_path="$(printf '%s' "$input" | grep -o '"file_path" *: *"[^"]*"' | head -n 1 |
  sed 's/.*"file_path" *: *"\([^"]*\)".*/\1/')"

[ -n "$file_path" ] || exit 0

case "$file_path" in
  *.*) ext="${file_path##*.}" ;;
  *) exit 0 ;;
esac

case "$ext" in
  ts | tsx | js | jsx | py | go | rs | rb | java | kt | swift | c | h | cpp | hpp | cs | php | ex | exs | sql | sh | nix | yaml | yml) ;;
  *) exit 0 ;;
esac

project_dir="${CLAUDE_PROJECT_DIR:-.}"
[ -d "$project_dir/.gitian" ] || exit 0

marker="${TMPDIR:-/tmp}/gitian-docs-nudge-${CLAUDE_SESSION_ID:-$PPID}"
[ -f "$marker" ] && exit 0

touch "$marker" 2>/dev/null || true

reminder='This repo documents code with gitian (@gitian annotations + docs/). If this change altered documented behavior, update the adjacent annotations/docs — see the gitian-docs skill.'

printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"%s"}}\n' "$reminder"
exit 0
