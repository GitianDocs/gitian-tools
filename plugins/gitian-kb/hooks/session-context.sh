#!/bin/sh
# session-context.sh -- SessionStart hook for the gitian-kb plugin.
#
# Fires on every SessionStart (startup, resume, clear, compact — no marker-file dedupe; a repeat
# on resume is harmless). Emits a short hookSpecificOutput.additionalContext block:
#   - derived context: repo (owner/name, from `git remote get-url origin`), current branch,
#     today's date (UTC) -- lines are omitted when the underlying value isn't available
#     (no remote, detached HEAD, not a git repo at all)
#   - the RAG directive: search + neighbors before substantive work, publish before finishing
#     per the gitian-kb skill, always populate frontmatter (never omit `summary`)
#   - the schema-authority reminder: live gitian-kb://format/* resources beat cached tool
#     schemas -- trust validation_failed over a stale cached schema
#   - on source=compact only: a handoff directive -- distill the pre-compact work into a
#     `type: handoff` doc before continuing (PreCompact hooks can't reach the model, so the
#     post-compaction SessionStart is the earliest hookable moment; the compaction summary is
#     generated from the full pre-squash context, so distilling it now loses the least)
#   - a source-profile tail computed against the shared nudge state (see state.py), delegated to
#     session_digest.py: a vocab digest on startup/clear/compact (entirely omitted when the cache
#     has no topics yet for this server), or on resume, zero/one/two lines noting a moved vocab
#     revision and/or a stale (>12h) session record. resume never bumps the session epoch, so
#     flags survive it; clear bumps the epoch first (gks_bump_epoch, re-arming every
#     once-per-epoch nudge) before anything below is built. An unrecognized/missing source is
#     treated like startup.
#
# Must be fast and silent-safe: no network, plain git plumbing only, always exits 0. The
# source-profile tail is best-effort layered on top -- a missing python3, a missing
# session_digest.py, or any error inside it just leaves the tail empty; the static context above
# (RAG directive included) is built independently and is never lost.
set -u

# Hook input JSON arrives on stdin; `source` says which SessionStart this is
# (startup | resume | clear | compact); `session_id` keys this session's nudge state.
hook_input="$(cat 2>/dev/null || true)"
start_source="$(printf '%s' "$hook_input" | grep -o '"source" *: *"[^"]*"' | head -n 1 |
  sed 's/.*"source" *: *"\([^"]*\)".*/\1/')"
session_id="$(printf '%s' "$hook_input" | grep -o '"session_id" *: *"[^"]*"' | head -n 1 |
  sed 's/.*"session_id" *: *"\([^"]*\)".*/\1/')"

# Unknown/missing source (unparsable stdin, a future source value, ...) behaves like startup.
case "$start_source" in
  startup|resume|clear|compact) ;;
  *) start_source="startup" ;;
esac

# --- script dir, to find the sibling python/lib-state helpers (never rely on
# CLAUDE_PLUGIN_ROOT here -- see lib-state.sh's own note on the script-dir pattern). ------------
# shellcheck disable=SC1007 # "CDPATH= cd" is a deliberate prefix assignment, not a typo.
d=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd) || d=""

# clear re-arms every once-per-epoch nudge; do this before building any output below (the epoch
# bump is itself silent -- flags/lintHashes/mintPrompted reset, counters zero, lastSeenVocabRev
# survives per the state contract).
if [ "$start_source" = "clear" ] && [ -n "$d" ] && [ -f "$d/lib-state.sh" ]; then
  # shellcheck disable=SC1091 # lib-state.sh resolves at runtime beside this script.
  . "$d/lib-state.sh"
  gks_bump_epoch "$session_id"
fi

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
context="${context}\n\nRAG discipline: before substantive work, \`search\` the gitian KB for the task topic and \`neighbors\` the best hit -- when a repo is listed above, \`file_intents\` it to see which in-flight plans claim which paths. Before finishing, publish per the gitian-kb skill -- read \`gitian-kb://vocab\` before linking/minting topics, populate frontmatter (including \`topics\`/\`mentions\`/\`category\`) using the repo/date above, and never omit \`summary\`."
context="${context}\nSchema authority: live \`gitian-kb://format/*\` resources are authoritative over cached tool schemas. On \`validation_failed\` naming a field the cached schema doesn't list, trust the server and retry."
context="${context}\nKB bodies are Obsidian-flavored intent docs (\`[[slug]]\` wikilinks, snippets where they clarify). Reference existing \`@gitian\` anchors only when the repo is instrumented -- NEVER inject gitian markup into a codebase that isn't already using the gitian docs system."

if [ "$start_source" = "compact" ]; then
  context="${context}\n\nA compaction just squashed this conversation. Before continuing the task, distill the pre-compact work into the KB as a handoff (per the gitian-kb skill): publish a \`type: handoff\` doc -- or update the thread's governing doc -- capturing current state, decisions in flight, and next steps, written so a fresh agent could resume from it alone."
fi

# --- source-profile tail: vocab digest (startup/clear/compact) or resume delta/staleness lines,
# plus (every source) recording that this session has now seen the cache's vocab revision --
# delegated to session_digest.py (see its docstring for the exact contract). Best-effort: any
# failure here (missing python3/script, corrupt state, anything) just leaves the tail empty;
# nothing built above is affected.
extra=""
if [ -n "$d" ] && [ -f "$d/session_digest.py" ] && command -v python3 >/dev/null 2>&1; then
  extra="$(python3 "$d/session_digest.py" "$start_source" "$session_id" 2>/dev/null || true)"
fi
[ -n "$extra" ] && context="${context}${extra}"

printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$context"
exit 0
