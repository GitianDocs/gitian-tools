#!/bin/sh
# session-context.sh -- SessionStart hook for the gitian-kb plugin.
#
# Fires on every SessionStart (startup, resume, clear, compact — no marker-file dedupe; a repeat
# on resume is harmless). Emits a short hookSpecificOutput.additionalContext block:
#   - derived context: repo (owner/name, from `git remote get-url origin`), current branch,
#     today's date (UTC), and this session's transcript -- lines are omitted when the underlying
#     value isn't available (no remote, detached HEAD, not a git repo at all, no transcript)
#   - the two transcript lines ([[kb-scribe-delegation]]): the session JSONL's absolute path and
#     the `python3 <plugin>/hooks/transcript_extract.py <path>` command that reduces it to the
#     conversation. Together they are what a brief hands kb-scribe so the primary never
#     re-emits a design it already wrote. Both are emitted only when the transcript AND the
#     extractor really exist -- a path that doesn't resolve is worse than no line at all.
#   - the delegation directive (the STATIC context, budgeted at <= 1200 chars -- see
#     tests/test_session_context.py's MAX_STATIC_CONTEXT_CHARS): dispatch kb-librarian for
#     orientation, brief kb-scribe at completion points, never auto-publish, `get` only what you
#     will act on, single-flight per role with follow-ups over SendMessage, the three brief fields
#     a subagent cannot see for itself (`kb` -- a label or `auto` --, `repo` as owner/name, branch)
#     plus what routing then does with them (docs/entries route on an org-owned `repo`, memories
#     never, neither `kb` nor `repo` lands in `home`, and the scribe reports `landed_in`), the
#     subagent fallback (read the skill's references/authoring.md and
#     work inline), and the one body rule that cannot move into an agent prompt: never inject
#     gitian markup into a repo that isn't already instrumented. The RAG/multi-KB/base_rev/
#     schema-authority paragraphs this replaces are NOT lost -- that discipline lives in the
#     agents (kb-scribe's prompt and the skill's reference files) now, which is the whole point:
#     the primary pays for none of it.
#   - on source=compact only: a handoff directive -- brief the scribe for a `type: handoff` doc
#     before continuing (PreCompact hooks can't reach the model, so the post-compaction
#     SessionStart is the earliest hookable moment; the compaction summary is generated from the
#     full pre-squash context, so distilling it now loses the least)
#   - a source-profile tail computed against the shared nudge state (see state.py), delegated to
#     session_digest.py: on resume, zero/one/two lines noting a moved vocab revision and/or a
#     stale (>12h) session record. There is deliberately NO vocab digest on startup/clear/compact
#     any more -- the agents read the vocabulary themselves, so injecting 25 topic lines into the
#     primary bought nothing. resume never bumps the session epoch, so flags survive it; clear
#     bumps the epoch first (gks_bump_epoch, re-arming every once-per-epoch nudge) before
#     anything below is built. An unrecognized/missing source is treated like startup.
#
# Must be fast and silent-safe: no network, plain git plumbing only, always exits 0. The
# source-profile tail is best-effort layered on top -- a missing python3, a missing
# session_digest.py, or any error inside it just leaves the tail empty; the static context above
# (delegation directive included) is built independently and is never lost.
set -u

# Hook input JSON arrives on stdin; `source` says which SessionStart this is
# (startup | resume | clear | compact); `session_id` keys this session's nudge state.
hook_input="$(cat 2>/dev/null || true)"
start_source="$(printf '%s' "$hook_input" | grep -o '"source" *: *"[^"]*"' | head -n 1 |
  sed 's/.*"source" *: *"\([^"]*\)".*/\1/')"
session_id="$(printf '%s' "$hook_input" | grep -o '"session_id" *: *"[^"]*"' | head -n 1 |
  sed 's/.*"session_id" *: *"\([^"]*\)".*/\1/')"
# `transcript_path` is this session's JSONL -- the scribe's only route to what was actually said.
transcript_path="$(printf '%s' "$hook_input" | grep -o '"transcript_path" *: *"[^"]*"' | head -n 1 |
  sed 's/.*"transcript_path" *: *"\([^"]*\)".*/\1/')"

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

# --- transcript + extractor (both or neither) --------------------------------------------------
# The extractor sits beside this script; CLAUDE_PLUGIN_ROOT is the fallback for the (unexpected)
# case where the script dir couldn't be resolved at all.
extractor=""
if [ -n "$d" ] && [ -f "$d/transcript_extract.py" ]; then
  extractor="$d/transcript_extract.py"
elif [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && [ -f "${CLAUDE_PLUGIN_ROOT}/hooks/transcript_extract.py" ]; then
  extractor="${CLAUDE_PLUGIN_ROOT}/hooks/transcript_extract.py"
fi
# Both lines or neither: a transcript with no extractor leaves the scribe a multi-megabyte log,
# and an extractor with no transcript is a dangling command.
if [ -z "$transcript_path" ] || [ ! -f "$transcript_path" ]; then
  extractor=""
fi
[ -n "$extractor" ] || transcript_path=""
transcript_path="$(json_escape "$transcript_path")"
extractor="$(json_escape "$extractor")"

context="gitian-kb session context:"
[ -n "$repo" ] && context="${context}\n- repo: ${repo}"
[ -n "$branch" ] && context="${context}\n- branch: ${branch}"
context="${context}\n- date (UTC): ${today}"
if [ -n "$transcript_path" ]; then
  context="${context}\n- transcript: ${transcript_path}"
  context="${context}\n- transcript-extract: python3 ${extractor}"
fi
context="${context}\n\nKB work is delegated: dispatch \`kb-librarian\` to orient before substantive work, brief \`kb-scribe\` (10-20 lines -- what happened and WHY) at completion points. You decide WHEN a publish is warranted and WHAT mattered; the scribe authors and revises. Never auto-publish."
context="${context}\n\`get\` directly only what you will act on; a one-off \`search\`/\`get\` stays inline."
context="${context}\nOne scribe and one librarian in flight; a follow-up on the same item goes to the LIVE agent via \`SendMessage\`. Every brief states \`kb\` (the KB the human named, else \`auto\`), \`repo\` as owner/name and the branch -- a subagent sees none of this context. Docs and journal entries with an org-owned \`repo\` route to that org's team KB, memories never; with neither \`kb\` nor \`repo\` a write can only land in \`home\`. The scribe reports \`landed_in\`. Pass the transcript lines when they hold the substance."
context="${context}\nA subagent (you cannot spawn one)? Read the gitian-kb skill's \`references/authoring.md\` and work inline."
context="${context}\nKB bodies are Obsidian-flavored intent docs (\`[[slug]]\` wikilinks). NEVER inject gitian markup into a codebase that isn't already using the gitian docs system."

if [ "$start_source" = "compact" ]; then
  context="${context}\n\nA compaction just squashed this conversation. Before continuing the task, brief \`kb-scribe\` for a \`type: handoff\` doc -- pass the transcript lines above -- capturing current state, decisions in flight, and next steps, written so a fresh agent could resume from it alone."
fi

# --- source-profile tail: on resume, the vocab-delta and/or staleness lines (nothing at all on
# startup/clear/compact since the vocab digest was retired), plus -- on EVERY source -- recording
# that this session has now seen the cache's vocab revision --
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
