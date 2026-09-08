#!/bin/sh
# docs-context.sh -- SessionStart hook for the gitian-docs plugin.
#
# Why a SessionStart hook exists at all: Claude Code elides a skill's description from the
# system prompt's skill listing when the listing budget is tight and the skill has no usage
# history, so a never-yet-invoked gitian-docs skill can be present-but-invisible -- the model
# never sees its "use when" cue and the plugin looks dead. This hook restores the cue, but only
# where it is warranted: it says nothing at all in a repo that doesn't already use gitian.
#
# Emits at most one line of additionalContext, naming the skill by its Skill-tool id so the
# model can invoke it directly. No network, no git beyond the shared probe, always exits 0.
set -u

# Hook input JSON arrives on stdin; `source` says which SessionStart this is
# (startup | resume | clear | compact); `session_id` keys this session's state files.
hook_input="$(cat 2>/dev/null || true)"

# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || d=""
[ -n "$d" ] && [ -f "$d/lib-instrumented.sh" ] || exit 0
# shellcheck disable=SC1091 # lib-instrumented.sh resolves at runtime beside this script.
. "$d/lib-instrumented.sh"

start_source="$(printf '%s' "$hook_input" | grep -o '"source" *: *"[^"]*"' | head -n 1 |
  sed 's/.*"source" *: *"\([^"]*\)".*/\1/')"
sid="$(gd_session_id "$hook_input")"

# /clear starts a fresh conversation on the same session id, so re-arm the PostToolUse nudge:
# the model that was told once has been replaced by one that hasn't. The cached probe verdict
# goes too, so a repo instrumented mid-session is picked up at the next natural boundary.
if [ "$start_source" = "clear" ] && [ -n "$sid" ]; then
  rm -f "$(gd_state_dir)/gitian-docs-nudge-$sid" "$(gd_state_dir)/probe-$sid" 2>/dev/null || true
fi

gd_prune

verdict="$(gd_instrumented "${CLAUDE_PROJECT_DIR:-.}" "$sid")"
[ -n "$verdict" ] || exit 0

case "$verdict" in
  gitian-dir) reason=".gitian/ present" ;;
  *) reason="@gitian annotations present" ;;
esac

context="gitian-docs: this repo is instrumented with gitian (${reason}). Before changing code here, invoke the gitian-docs skill (Skill tool: gitian-docs:gitian-docs) and apply its duty table -- keep @gitian annotations and paired docs/ files in sync in the same change; /gitian-docs:instrument runs a pass over the current diff. NEVER add gitian markup to a repo that isn't already using it."

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$context"
exit 0
