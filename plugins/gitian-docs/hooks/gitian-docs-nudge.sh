#!/bin/sh
# gitian-docs-nudge.sh -- PostToolUse hook for the gitian-docs plugin.
#
# Fires after Edit/Write/MultiEdit/NotebookEdit/Bash (see hooks/hooks.json). Must be fast and
# quiet: it exits 0 with no output unless ALL of the following hold:
#   (a) the tool call actually wrote code -- a code-extension path for the file tools, or a
#       write signature plus a code-file token (or a `git commit`) for Bash. Bash matters
#       because in auto/accept-edits modes the model routinely edits through sed/heredocs, which
#       an Edit-only matcher never sees;
#   (b) the project is gitian-instrumented (a .gitian/ directory, or `@gitian:` tags in tracked
#       non-markdown source -- see lib-instrumented.sh);
#   (c) this session hasn't already been nudged (marker file under the plugin state dir, keyed
#       on the session_id from the hook payload).
# When all three hold, it emits a one-line PostToolUse additionalContext reminder pointing at
# the gitian-docs skill, then writes the marker so it only fires once per session.
set -u

input="$(cat 2>/dev/null || true)"

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || d=""
[ -n "$d" ] && [ -f "$d/lib-instrumented.sh" ] || exit 0
# shellcheck disable=SC1091 # lib-instrumented.sh resolves at runtime beside this script.
. "$d/lib-instrumented.sh"

json_str() {
  printf '%s' "$input" | grep -o "\"$1\" *: *\"[^\"]*\"" | head -n 1 |
    sed "s/.*\"$1\" *: *\"\([^\"]*\)\".*/\1/"
}

tool_name="$(json_str tool_name)"

# Extensions that count as "code" for both gates below. Markdown is deliberately absent: a
# prose edit is not what the annotation/doc sync duty is about.
code_ext='ts|tsx|js|jsx|py|go|rs|rb|java|kt|swift|c|h|cpp|hpp|cs|php|ex|exs|sql|sh|nix|yaml|yml|ipynb'

case "$tool_name" in
  Edit | Write | MultiEdit | NotebookEdit)
    if [ "$tool_name" = "NotebookEdit" ]; then
      target="$(json_str notebook_path)"
    else
      target="$(json_str file_path)"
    fi
    [ -n "$target" ] || exit 0
    case "$target" in
      *.*) ;;
      *) exit 0 ;;
    esac
    printf '%s' "${target##*.}" | grep -Eq "^($code_ext)$" || exit 0
    ;;
  Bash)
    command_line="$(json_str command)"
    [ -n "$command_line" ] || exit 0
    eligible=0
    # A commit is when documentation gets reconciled, so it qualifies on its own.
    case "$command_line" in
      *"git commit"*) eligible=1 ;;
    esac
    if [ "$eligible" -eq 0 ]; then
      # Otherwise: a write signature AND something that looks like a code file being written.
      # stderr redirects (`2>/dev/null`, `2>&1`) and `>/dev/null` are read-only idioms, not
      # writes -- drop them first so a grep over a .ts file doesn't spend the session's nudge.
      command_line="$(printf '%s' "$command_line" |
        sed -e 's/2>&[0-9]//g' -e 's/2>[^ ]*//g' -e 's#>/dev/null##g')"
      case "$command_line" in
        *"sed -i"* | *"tee "* | *">"* | *"git apply"* | *"patch "*)
          if printf '%s' "$command_line" |
            grep -Eq '[A-Za-z0-9_./-]+\.('"$code_ext"')([^A-Za-z0-9]|$)'; then
            eligible=1
          fi
          ;;
      esac
    fi
    [ "$eligible" -eq 1 ] || exit 0
    ;;
  *) exit 0 ;;
esac

sid="$(gd_session_id "$input")"
[ -n "$(gd_instrumented "${CLAUDE_PROJECT_DIR:-.}" "$sid")" ] || exit 0

gd_prune

marker="$(gd_state_dir)/gitian-docs-nudge-$sid"
[ -f "$marker" ] && exit 0
touch "$marker" 2>/dev/null || true

reminder='This repo documents code with gitian (@gitian annotations + docs/). If this change altered documented behavior, update the adjacent annotations/docs now -- invoke the gitian-docs skill (gitian-docs:gitian-docs) or run /gitian-docs:instrument.'

printf '{"hookSpecificOutput":{"hookEventName":"PostToolUse","additionalContext":"%s"}}\n' "$reminder"
exit 0
