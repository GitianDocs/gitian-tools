#!/bin/sh
# spec-context.sh -- SessionStart hook for the gitian-spec plugin.
#
# gitian-kb's own SessionStart hook already injects repo/branch/date context plus the RAG
# discipline, so this hook deliberately duplicates NONE of that (no git calls at all). It emits:
#   - one spec-routing line: long-form work docs (specs, plans, designs, brainstorms, handoffs,
#     session notes) are KB deliverables -- publish_doc/publish_entry per the gitian-spec skill,
#     never loose markdown files
#   - a companion warning ONLY when the gitian-kb plugin is missing from
#     installed_plugins.json -- gitian-spec ships no MCP config of its own (single-connection
#     design), so without gitian-kb there are no gitian tools to publish with
#
# Must be fast and silent-safe: no network, no git, static strings only, always exits 0.
set -u

# Swallow stdin (hook input JSON) so the pipe never blocks; nothing in it is needed here.
cat >/dev/null 2>&1 || true

context="gitian-spec: specs, plans, designs, brainstorms, handoffs, and session notes are KB deliverables -- author them via publish_doc/publish_entry per the gitian-spec skill, never as loose markdown files."

# Companion detection: any marketplace's gitian-kb install counts. When the registry file is
# missing or unreadable we cannot tell, so stay quiet rather than warn wrongly.
installed="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/installed_plugins.json"
if [ -f "$installed" ] && ! grep -q '"gitian-kb@' "$installed" 2>/dev/null; then
  context="${context}\n\nWARNING: the required companion plugin gitian-kb is not installed. gitian-spec ships no MCP config of its own (single-connection design), so the gitian tools are unavailable until you run: claude plugin install gitian-kb@gitian-tools"
fi

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$context"
exit 0
