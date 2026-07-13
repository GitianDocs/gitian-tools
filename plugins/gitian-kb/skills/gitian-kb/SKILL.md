---
name: gitian-kb
description: Use when starting work (to query prior decisions/context from the gitian Knowledge Base) and when finishing meaningful work — a settled design, a written plan, a durable fact worth remembering, a handoff, a day worth recording, or a landed feature — to publish distilled knowledge via the gitian MCP tools.
allowed-tools: Bash(git remote get-url:*), Bash(git branch:*), Bash(date:*)
---

# gitian-kb

The gitian Knowledge Base (KB) is the durable memory git history and code comments don't give you: design rationale, decisions, status, next steps. This skill is the discipline for reading it before you start work and writing to it when work reaches a shape worth keeping. The KB tools teach the format themselves (via resources); this skill teaches when and how to use them.

## Preloaded context

- Repo remote: !`git remote get-url origin 2>/dev/null || echo "(none)"`
- Branch: !`git branch --show-current 2>/dev/null || echo "(none)"`
- Today (UTC): !`date -u +%Y-%m-%d`

Preloaded so you never spend tool calls deriving them: set `repo` by normalizing the remote to `owner/name` (explicit `null` if "(none)" — never guess), use today's date for entry `date` fields, and populate frontmatter from these values instead of re-running git.

## Picking a primitive

Pick by shape, not size:

- **memory** — one atomic, durable fact: a preference, a gotcha, a project fact. Small and stable, even if the investigation that produced it took days.
- **doc** — a long-form artifact with a lifecycle: `spec`, `plan`, `design`, `handoff`, or `recap`, carrying the full manifest (status, impl_status, next_steps). Use it once the work has shape and needs tracking over time.
- **entry** — a dated journal record for a scope (`work` or `personal`): what happened, worth recalling later, even if it's short.

A multi-day effort with status and next steps is a doc even if the write-up is one paragraph; a single durable fact is a memory even if it took a long investigation to learn.

## The loop

1. **Orient** — before any substantive work, not just before writing: `search` (or `list`) the KB for the topic, then call `neighbors` on the best hit. This is RAG at work-start, not a publish-time formality — the item you're about to create may already exist under a different slug you haven't thought of, and the likeness graph surfaces adjacent decisions a keyword search alone would miss. Working in a repo? `file_intents` the repo too — it lists which in-flight docs claim which paths; on overlap with what you're about to touch, `get` the contending doc before proceeding. `include_landed: true` widens that same call into "who has reworked this area before" — deactivated (landed/abandoned) plans included, not just what's in flight.
2. **Thread** — read the matching `gitian-kb://format/<primitive>` resource, then adopt what's already decided: don't re-litigate a settled design, cross-link into it via `related` (slugs) instead of duplicating it. Before heavily editing a doc you didn't write, pull it with `get` and skim `history` to see how it evolved. Call `neighbors` on it too — the likeness graph surfaces adjacent decisions you wouldn't have thought to `search` for.
3. **Publish** — call the right tool (`publish_memory` / `publish_doc` / `publish_entry`) with the full manifest, not a partial one.
4. **Confirm** — every `publish_*` call returns a `url`; surface it to the user: "published → `<url>`". (`retract_item` returns `{slug, rev, tombstoned}` — no url.)

Retract obsolete items with `retract_item` rather than trying to delete their content — it appends a tombstone revision. History survives, and re-publishing the same slug un-deletes it.

A `publish_*` result may also carry `suggested_related`: up to 3 likeness neighbors the server noticed but you didn't already list in `related`. Review it on every publish — when a suggestion is genuinely related, re-publish the same slug with it added to `related` rather than ignoring it; ground truth improves the likeness graph for everyone who calls `neighbors` after you.

Worked example: fixing a flaky CI failure. `search "flaky auth"` turns up nothing; read `gitian-kb://format/memory`; fix the bug; `publish_memory` with slug `ci-flaky-oauth-token`, `type: reference`; tell the user "published → `<url>`" from the response.

## When to publish — trigger table

| Trigger | Tool | Notes |
|---|---|---|
| Design settled | `publish_doc` | `type: spec` or `type: design` |
| Plan written | `publish_doc` | `type: plan` |
| Durable fact learned | `publish_memory` | a preference, gotcha, or project fact worth recalling next session |
| Meaningful event (pivots included) | `publish_entry` | append to today's running entry as it happens — see the running-record section below |
| Handing off mid-stream | `publish_doc` | `type: handoff` |
| Conversation pivots off a thread | `publish_doc`/`publish_memory` + `publish_entry` | a pivot ends the old thread as surely as finishing it — publish or update its governing item *before* engaging the new topic, and fold the pivot into today's journal entry |
| Context compacted | `publish_doc` | `type: handoff` — right after a compaction, distill the summary plus what you still hold into a handoff a fresh agent could resume from (the session hook reminds you); before a *manual* `/compact`, run `/gitian-kb:handoff` to capture state pre-squash |
| Feature landed | `publish_doc` (`type: recap`) **and** flip the feature doc's `status` to a terminal value | do both — a recap without the status flip leaves the KB stale |

## Companion: gitian-spec

When the gitian-spec plugin is installed, long-form *authoring* — being asked to write a spec,
plan, design, brainstorm, handoff, or session note, and documenting what a landed feature shipped
— follows that skill's discipline (scan-before-write, manifest field derivation, commits-list
conventions, the implementation-recap checklist and its in-body-vs-separate-doc placement rule).
This skill keeps orientation (RAG at work-start), completion-point distillation, the journal, and
memories. Same tools, same `gitian-kb://format/*` authority either way — gitian-spec ships no MCP
config of its own and rides this plugin's single connection.

## Orient first

Read the matching resource before the *first* use of each publish tool in a session — don't guess the shape:

- `gitian-kb://format/overview` — the three primitives, upsert/versioning rules, null-not-omitted
- `gitian-kb://format/memory` — memory fields and slug conventions
- `gitian-kb://format/doc` — the full doc manifest, enums, distillation and recap guidance
- `gitian-kb://format/entry` — the journal format and the running-record discipline

`search` before inventing a new slug. The same topic may already have an item under a name you didn't guess — re-publish the *same* slug to update it (appends a revision); only mint a new slug for a genuinely new topic.

## Schema authority

Installed tool schemas are a cached snapshot, not the live contract — they can lag behind the
server (a past cache once omitted `summary`/`repo` entirely, so every publish built strictly from
the cached schema failed `validation_failed`). The live `gitian-kb://format/*` resources and the
server's own validation errors are always authoritative. If `validation_failed` names a field the
cached tool schema didn't mention, that isn't a bug in your call — trust the server, add the
field, and retry.

## Publishing rules

- Every schema key must be present in the call — explicit `null` (or `[]` for list fields) when a value is genuinely unknown, never omit the key. A thin publish that drops required keys is rejected.
- Never write `created_at`, `updated_at`, `rev`, or `author` yourself — the platform stamps these from the token and the revision; they aren't yours to set.
- Slugs are stable, lowercase-kebab, and name the thing (`auth-token-nullable`, not `note-1`). Entries are the exception: they take `date` + `scope`, never a slug — the platform derives one from both.
- Slugs share one namespace per owner across all three primitives — a `memory` and a `doc` can't reuse the same slug. Pick something specific enough not to collide, and `search` first so you don't collide silently.
- An identical re-publish returns `unchanged: true`. That is success, not an error — don't retry it or treat it as a failure.
- **Populate frontmatter — don't default to null.** `project`, `repo`, and `tags` must be filled whenever they're derivable, not left null out of habit. The SessionStart hook context (repo, branch, date) gives you what you need for `repo` at the top of the session; set `project` from the obvious repo/workspace name. Explicit `null` is only for work that's genuinely not project- or repo-bound — never a shortcut. Always include `summary`, especially on memories, where it's the only preview a list view shows.
- `warnings` on a successful publish are advice to act on, not blockers. Seven codes:
  - `no_tags` — no tags supplied; add 1-3 to aid retrieval
  - `no_project` — `project` is null; derive it from context or confirm this isn't project-bound
  - `no_repo` — `repo` is null; derive it from `git remote get-url origin` (the SessionStart hook already surfaces this) or confirm the work isn't repo-bound
  - `landed_without_commits` — `status: landed` but `commits` is empty; add the landing commit(s)/PR
  - `impl_done_status_open` — `impl_status: done` but `status` is still draft/designing/in-progress/blocked; reconcile before closing out
  - `terminal_with_next_steps` — `status` is terminal but `next_steps` is non-empty; confirm they still apply
  - `plan_without_files` — an active plan with a `repo` but empty `files`; declare the paths the plan will touch (trailing `/` = subtree) so parallel agents can detect contention
- On `validation_failed`, fix every listed `issue` and retry — the error's `format_resource` field names the exact guide to re-read.
- `contention` on a successful `publish_doc` means another active doc declares overlapping `files` — read it (`get`), coordinate or narrow scope, and cross-link it in `related`. If the hit carries a non-null `owner` (a teammate's plan on a shared org repo, not your own), pass that login as `get`'s `owner` param — a bare `get slug` looks up *your own* item at that slug (or `not_found`), not theirs. `file_intents` hits carry the same `owner` field for the same reason.
- **Org-wide visibility** is read-time and repo-scoped: when a repo belongs to a gitian org you're seated + entitled in, `file_intents`/`contention` widen from your own rows to every currently-seated member's rows on that repo, and `get` with `owner` can read a teammate's doc under the grant its frontmatter declares (`repo` + `files`) — nothing here is a separate opt-in or a different tool.
- `body` is distilled content — decisions made, the rationale behind them, alternatives considered and rejected — not a transcript of the conversation or a chronological log of messages. Write what a future reader needs to understand and trust the outcome.
- Set `repo` to the working repository as `owner/name` (derive it from `git remote get-url origin`); explicit `null` when the work isn't repo-bound or there's no remote — never guess. The repo doesn't need to be connected to gitian; identity is late-binding.

## Writing bodies

Bodies are Obsidian-flavored intent documentation — why the thing is the way it is, not a transcript. Link related KB items inline with `[[slug]]` wikilinks (they resolve in the UI and strengthen the likeness graph), structure with headings, and include short code snippets where they say it better than prose. Reference code where the knowledge lives: in a repo already instrumented with gitian docs (a `.gitian/` config directory, `@gitian` annotations, paired `docs/` files), point at those anchors — an annotation id, a doc path — instead of duplicating their content; in any other repo, reference files and symbols plainly. **Never add `@gitian` annotations or any gitian markup to a codebase that isn't already using the gitian docs system** — publishing to the KB never licenses editing code comments; in-code instrumentation is opt-in via the gitian-docs plugin only.

## Terminal-state discipline

Before telling the user work is done, abandoned, or paused, re-publish the governing doc with the updated `status` (and `impl_status`) — the platform stamps `updated_at` for you, so this is the only step you owe it. A landed feature gets **both** the status flip and a `recap` doc; shipping one without the other leaves the KB half-updated and misleads whoever reads it next.

Worked example: a feature branch merges. Re-publish the design/plan doc's slug — the full manifest again, with `status: landed`, `impl_status: done`, `commits: [<sha>]` updated — then `publish_doc` a new `type: recap` covering what shipped, the key decisions, gotchas discovered, and any deferred follow-ups.

## The journal is a running record (entries)

The day's entry is a running record of meaningful events, appended to as they happen — not a day's-end summary gated behind a whole-day bar. When something meaningful happens mid-session — a feature lands, a real blocker appears, a decision settles, a finding surfaces, or the conversation pivots off a thread — re-publish today's entry with it folded in. One entry exists per scope per day; re-publishing the same `date` + `scope` appends a revision, so appending is cheap and safe.

The bar for "meaningful" is what a teammate would care to hear at standup — a pivot, a diagnosis, a settled design all clear it; routine mechanical work (a rename, a re-run, a dependency bump) does not. The journal records the day a colleague would want to catch up on, not a command log.

## Never auto-publish

Publish at natural completion points or on explicit user intent, full stop. Never publish silently, never in bulk, and never mid-task on a timer — the meaningful-event bar and the discipline above only hold if every publish is a deliberate call, not a background habit.

Natural completion points: a design conversation converges, a plan is finished and about to be handed to implementation, a feature merges, a work session wraps up worth an entry, the conversation pivots off a thread, or a compaction has just squashed (or a manual `/compact` is about to squash) undistilled context. If none of those has happened, don't publish yet — keep working and revisit the trigger table later.
