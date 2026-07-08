#!/bin/sh
# session-context.sh -- SessionStart hook for the gitian-kb plugin.
#
# Fires on every SessionStart (startup and resume alike — no marker-file dedupe; a repeat on
# resume is harmless). Emits a short hookSpecificOutput.additionalContext block:
#   - derived context: repo (owner/name, from `git remote get-url origin`), current branch,
#     today's date (UTC) -- lines are omitted when the underlying value isn't available
#     (no remote, detached HEAD, not a git repo at all)
#   - the RAG directive: search + neighbors before substantive work, publish before finishing
#     per the gitian-kb skill, always populate frontmatter (never omit `summary`)
#   - the schema-authority reminder: live gitian-kb://format/* resources beat cached tool
#     schemas -- trust validation_failed over a stale cached schema
#
# Must be fast and silent-safe: no network, plain git plumbing only, always exits 0.
set -u

project_dir="${CLAUDE_PROJECT_DIR:-.}"

# --- repo: normalize git@/https/ssh remote forms down to "owner/name" ------------------------
remote_url="$(git -C "$project_dir" remote get-url origin 2>/dev/null || true)"
repo=""
if [ -n "$remote_url" ]; then
  candidate="$(printf '%s' "$remote_url" |
    sed -E 's#^git@([^:]+):#https://\1/#' |
    sed -E 's#\.git$##' |
    sed -E 's#^[a-z]+://[^/]+/##')"
  # Only trust it if it reduced to a clean single owner/name pair (one slash, no empties) --
  # anything else (subgroups, malformed URLs) isn't safe to hand the model as `repo`.
  case "$candidate" in
    */*/*) ;; # more than one slash -- reject
    */*) repo="$candidate" ;;
  esac
fi

# --- branch (empty on detached HEAD or no commits yet -- just omit the line) -----------------
branch="$(git -C "$project_dir" branch --show-current 2>/dev/null || true)"

# --- JSON-escape dynamic values BEFORE interpolation --------------------------------------------
# Git permits `"` in branch names; unescaped it would emit malformed JSON and the whole
# context injection would be silently dropped. Only the dynamic values are escaped -- the
# static template's literal \n sequences must remain JSON newline escapes.
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}
repo="$(json_escape "$repo")"
branch="$(json_escape "$branch")"

# --- today's date, UTC -------------------------------------------------------------------------
today="$(date -u +%Y-%m-%d)"

context="gitian-kb session context:"
[ -n "$repo" ] && context="${context}\n- repo: ${repo}"
[ -n "$branch" ] && context="${context}\n- branch: ${branch}"
context="${context}\n- date (UTC): ${today}"
context="${context}\n\nRAG discipline: before substantive work, \`search\` the gitian KB for the task topic and \`neighbors\` the best hit. Before finishing, publish per the gitian-kb skill -- populate frontmatter using the repo/date above, and never omit \`summary\`."
context="${context}\nSchema authority: live \`gitian-kb://format/*\` resources are authoritative over cached tool schemas. On \`validation_failed\` naming a field the cached schema doesn't list, trust the server and retry."

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$context"
exit 0
