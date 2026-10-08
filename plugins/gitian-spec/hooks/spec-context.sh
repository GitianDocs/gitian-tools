#!/bin/sh
# spec-context.sh -- SessionStart hook for the gitian-spec plugin.
#
# gitian-kb's own SessionStart hook already injects repo/branch/date context plus the delegation
# directive, so this hook deliberately duplicates NONE of that (no git calls at all). It emits:
#   - one spec-routing line: long-form work docs (specs, plans, designs, brainstorms, handoffs,
#     session notes) are KB deliverables -- scanned, then briefed to the kb-scribe subagent per
#     the gitian-spec skill, never loose markdown files
#   - a companion warning ONLY when the gitian-kb plugin is missing from
#     installed_plugins.json -- gitian-spec ships no MCP config of its own (single-connection
#     design), so without gitian-kb there are no gitian-kb tools to publish with
#
# NOTHING AT ALL in a delegated session: when the payload carries `agent_id` (set only when a hook
# fires inside a subagent) or `agent_type` (the field SessionStart documents -- a subagent's type,
# or the name a `claude --agent` session runs as). The one line says "brief the kb-scribe
# subagent", which a subagent has no tool to do; gitian-kb's own hook carries the inline-work
# pointer such a session needs.
#
# Must be fast and silent-safe: no network, no git, static strings only, always exits 0.
set -u

# Read stdin (hook input JSON) whole, so the pipe never blocks; only the two agent fields matter.
hook_input="$(cat 2>/dev/null || true)"
agent_id="$(printf '%s' "$hook_input" | grep -o '"agent_id" *: *"[^"]*"' | head -n 1 |
  sed 's/.*"agent_id" *: *"\([^"]*\)".*/\1/')"
agent_type="$(printf '%s' "$hook_input" | grep -o '"agent_type" *: *"[^"]*"' | head -n 1 |
  sed 's/.*"agent_type" *: *"\([^"]*\)".*/\1/')"
if [ -n "$agent_id" ] || [ -n "$agent_type" ]; then
  exit 0
fi

context="gitian-spec: specs, plans, designs, brainstorms, handoffs, and session notes are KB deliverables -- scan the KB, then brief the kb-scribe subagent (pass the session transcript) to author and publish the doc per the gitian-spec skill, never a loose markdown file."

# Companion detection: any marketplace's gitian-kb install counts. When the registry file is
# missing or unreadable we cannot tell, so stay quiet rather than warn wrongly.
installed="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/installed_plugins.json"
if [ -f "$installed" ] && ! grep -q '"gitian-kb@' "$installed" 2>/dev/null; then
  context="${context}\n\nWARNING: the required companion plugin gitian-kb is not installed. gitian-spec ships no MCP config of its own (single-connection design), so the gitian-kb tools are unavailable until you run: claude plugin install gitian-kb@gitian-tools"
fi

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$context"
exit 0
