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

1. **Orient** — before any substantive work, not just before writing: read `gitian-kb://vocab` first (the live topic + category vocabulary — slug, description, degree for topics; slug, name, routing prompt for categories), then `search` (or `list`) the KB for the topic, then call `neighbors` on the best hit. This is RAG at work-start, not a publish-time formality — the item you're about to create may already exist under a different slug you haven't thought of, the vocab may already have a topic naming what you're about to link, and `neighbors`' topic-derived neighborhood surfaces adjacent decisions a keyword search alone would miss. Working in a repo? `file_intents` the repo too — it lists which in-flight docs claim which paths; on overlap with what you're about to touch, `get` the contending doc before proceeding. `include_landed: true` widens that same call into "who has reworked this area before" — deactivated (landed/abandoned) plans included, not just what's in flight.
2. **Thread** — read the matching `gitian-kb://format/<primitive>` resource, then adopt what's already decided: don't re-litigate a settled design, cross-link into it via `related` (slugs) instead of duplicating it. Before heavily editing a doc you didn't write, pull it with `get` and skim `history` to see how it evolved. Call `neighbors` on it too — its topic-derived `why` surfaces adjacent decisions you wouldn't have thought to `search` for.
3. **Publish** — call the right tool (`publish_memory` / `publish_doc` / `publish_entry`) with the full manifest, not a partial one, `topics`/`mentions`/`category` included (see **Topics & categories** below). Updating today's journal entry is the one exception: use `append_entry` (see **The journal is a running record** below) — a small, targeted call, not a full-manifest publish.
4. **Confirm** — every `publish_*` call returns a `url`; surface it to the user: "published → `<url>`". (`retract_item` returns `{slug, rev, tombstoned}` — no url.)

Retract obsolete items with `retract_item` rather than trying to delete their content — it appends a tombstone revision. History survives, and re-publishing the same slug un-deletes it.

A `publish_*` result may also carry `suggested_topics`: up to 5 existing topics the server noticed read close to what you just wrote but that you didn't already link in `topics`/`mentions`. Review it on every publish — when a suggestion is genuinely on-topic, re-publish the same slug with it added rather than ignoring it; precise linking is what keeps `neighbors` useful for everyone who calls it after you.

Worked example: fixing a flaky CI failure. `search "flaky auth"` turns up nothing; read `gitian-kb://format/memory`; fix the bug; `publish_memory` with slug `ci-flaky-oauth-token`, `type: reference`; tell the user "published → `<url>`" from the response.

## When to publish — trigger table

| Trigger | Tool | Notes |
|---|---|---|
| Design settled | `publish_doc` | `type: spec` or `type: design` |
| Plan written | `publish_doc` | `type: plan` |
| Durable fact learned | `publish_memory` | a preference, gotcha, or project fact worth recalling next session |
| Meaningful event (pivots included) | `append_entry` | append to today's running entry as it happens — see the running-record section below; `publish_entry` remains for a full rewrite |
| Handing off mid-stream | `publish_doc` | `type: handoff` |
| Conversation pivots off a thread | `publish_doc`/`publish_memory` + `append_entry` | a pivot ends the old thread as surely as finishing it — publish or update its governing item *before* engaging the new topic, and fold the pivot into today's journal entry |
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

## Delegating mechanical work: kb-librarian

Some of what this skill asks for is judgment (what to write, which topics to link); the rest is
read-heavy or purely mechanical, and shouldn't cost the primary model's context. When this plugin's
`kb-librarian` subagent (`agents/kb-librarian.md`, haiku-pinned) is available, delegate to it along
this division of labor:

| Operation | Who | Why |
|---|---|---|
| Rev-1 authorship + its publish call | Primary, inline | Emission = authorship; overhead minimal |
| Mechanical doc revisions (flips, merges) | kb-librarian runner | The ballast lives here (~95% savings) |
| Journal appends | `append_entry` server-side | Highest frequency; clobber-proof; all clients |
| Orientation sweep, vocab refresh | kb-librarian | Read-heavy, mechanical, context-fat |
| Topic/category choice, body content | Primary, always | Judgment + fragmentation risk |

Body authorship and topic/category choice are never delegated — a KB doc's value is that the
author was there, and under auto-minting any slug a weaker model invents becomes a live topic, so
the fragmentation risk stays with the primary. A single resource read or one-off tool call also
stays inline — spawning a subagent costs more than the call it would save. Reach for
`kb-librarian` for the shape of work in its two right-hand rows: dispatch it for the orientation
sweep at session start instead of spending 4-6 calls and their full outputs inline, dispatch it for
a vocab-delta refresh when `vocab_rev` has moved (see below), and hand it mechanical revisions as
an exact delta ("flip status to landed, add these commits, change nothing else") rather than
re-emitting a whole manifest yourself.

## Orient first

Read `gitian-kb://vocab` (see **Topics & categories** below) plus the matching format resource before the *first* use of each publish tool in a session — don't guess the shape:

- `gitian-kb://vocab` — the live topic vocabulary and category routing prompts, as JSON
- `gitian-kb://format/overview` — the three primitives, upsert/versioning rules, null-not-omitted
- `gitian-kb://format/memory` — memory fields and slug conventions
- `gitian-kb://format/doc` — the full doc manifest, enums, distillation and recap guidance
- `gitian-kb://format/entry` — the journal format and the running-record discipline

`search` before inventing a new slug. The same subject may already have an item under a name you didn't guess — re-publish the *same* slug to update it (appends a revision); only mint a new slug for a genuinely new subject.

## Staying in sync mid-session (`vocab_rev`)

Every successful tool response carries `vocab_rev` — an owner-scoped counter that bumps on any
vocabulary write: category CRUD, a topic mint/promote/tombstone, including an auto-mint that
happened as a side effect of someone else's publish. Track the value you last saw. **If a later
call's `vocab_rev` differs from it, re-read `gitian-kb://vocab` before your next publish** — the
vocabulary changed mid-session, and publishing against a stale read risks linking a slug that no
longer means what you think, minting a near-duplicate of something a teammate (or an earlier call
in this same session) just curated, or missing a category that now fits. This is cheap: the vocab
resource is small and the freshness signal rides on calls you're already making — no polling, no
extra round trip. When the drift is more than "one topic changed," dispatch `kb-librarian` for the
vocab-delta refresh instead of re-reading and re-diffing it yourself.

## Topics & categories

Doc-doc relatedness is entirely topic-derived now — there's no other correlation signal besides an explicit `related`/wikilink. Link deliberately, every publish:

- **`topics`** (primary tier, "this item is *about* X") — advise 1-3 per item. **`mentions`** (secondary tier, "this item *touches* X" without being about it) — as many as apply. A slug in both collapses to primary.
- **Prefer existing topics.** `gitian-kb://vocab` lists every live topic with its description, degree, and class (`curated`/`organic`) — link to what's already there before considering a new one. A vague, catch-all topic (or one linked to nearly everything) contributes almost nothing to relatedness by construction (informativeness falls as membership grows), so precision beats coverage.
- **Freshness discipline.** `gitian-kb://vocab` also carries `freshness` (0-1) and `dormant` (`freshness` below a fixed threshold) per topic, sorted freshness-descending — curated topics are always `1.0` (a human confirmation never spoils); an organic topic nobody has linked in a while quietly decays, and any new item linking it revives it instantly, no ceremony required. When more than one live topic reads as on-topic, prefer the fresher one; treat the dormant tail as lower-priority, not gone — a dormant topic is due for a second look (re-link deliberately, or let it keep decaying) rather than an automatic pick. This is a display/routing signal only — it never changes what a link means, only which topic to reach for first.
- **Aliases resolve transparently.** A merged topic never appears as its own entry in `gitian-kb://vocab` — only its canonical does, carrying `aliases: [...]` for every slug now merged into it. A link naming an alias still works (it resolves to the canonical on every read — degree, relatedness, `neighbors`, `topic`), but prefer linking the canonical spelling once you see it in the vocab response rather than perpetuating the old name. Merge/unmerge itself is a human call in the review queue, not a tool call — nothing here mints or removes an alias on your behalf.
- **Cold domain: mint, don't default.** When nothing in `gitian-kb://vocab` fits the domain of what you're publishing — a genuinely new area of work the vocabulary hasn't caught up to yet — mint 1-3 genuine concept topics with real descriptions via `publish_topic` (not a bare auto-mint left undescribed). Never default to a topic that just names the project or repo (see `project_name_topic` below) and never default to publishing with empty `topics` (see `no_topics` below) — an empty or project-name-only vocab is exactly the situation this rule exists for, not an excuse to skip linking.
- **A novel slug auto-mints — deliberately, not for free.** Naming a topic slug in `topics`/`mentions` that isn't in the vocab yet is never rejected and never inert: it auto-mints as `organic` and is live in relatedness immediately, no `publish_topic` call required, and the response carries an informational `organic_topics_minted` warning naming what got minted. That lowered floor is not a license to invent freely — every fresh mint is a permanent vocabulary entry someone (a human, or `kb-librarian`'s vocab-delta reports) eventually has to make sense of, and a slug chosen carelessly is exactly how synonym fragmentation creeps in. Check `gitian-kb://vocab` for an existing slug that already names the concept before typing a new one; only mint when the concept is genuinely absent. `publish_topic` still exists to attach a real description to a topic (mints organic too, or refreshes an existing one's description) — call it when the concept deserves documentation, not just to make a link "count" (it never did; class governs trust, not whether a link scores at all). The one slug family that stays inert is a **tombstoned** one: a user veto is never overruled by frontmatter, so the link is stored but excluded from relatedness (advisory `tombstoned_topics` warning) until someone deliberately re-mints it via `publish_topic`.
- **`category`** — at most one, `null` if none. Pick from `gitian-kb://vocab`'s categories using their routing prompts; an unknown category slug gets the same late-binding treatment (`unknown_category` warning). Categories are authored in the `/kb` UI, not minted over MCP.
- **Update-over-create bias.** Before minting a brand-new `doc` slug, check whether an existing *active* doc already owns the same primary topics — `list({topic: "<slug>"})` or the `topic` tool's member list — and update that doc instead of publishing a near-duplicate. The server backstops this with an advisory `consider_update` warning on a rev-1 doc mint whose primary topics heavily overlap an existing doc's, but don't rely on the backstop catching everything; check first.
- **`neighbors`** on a slug returns each hit's `why.topics` (the shared topics driving the score, richest first) and `why.explicit` (non-null when an explicit link floors the weight at 1.0) — use it to understand *why* something surfaced, not just *that* it did.
- **`topic`** (hub view) and **`publish_topic`**/**`retract_topic`** (mint/update/tombstone) round out the toolset for working with the vocabulary directly — see their tool descriptions for the exact shapes.

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
- `warnings` on a successful publish are advice to act on, not blockers. Sixteen codes:
  - `no_tags` — no tags supplied; add 1-3 to aid retrieval
  - `no_project` — `project` is null; derive it from context or confirm this isn't project-bound
  - `no_repo` — `repo` is null; derive it from `git remote get-url origin` (the SessionStart hook already surfaces this) or confirm the work isn't repo-bound
  - `landed_without_commits` — `status: landed` but `commits` is empty; add the landing commit(s)/PR
  - `impl_done_status_open` — `impl_status: done` but `status` is still draft/designing/in-progress/blocked; reconcile before closing out
  - `terminal_with_next_steps` — `status` is terminal but `next_steps` is non-empty; confirm they still apply
  - `plan_without_files` — an active plan with a `repo` but empty `files`; declare the paths the plan will touch (trailing `/` = subtree) so parallel agents can detect contention
  - `organic_topics_minted` — a `topics`/`mentions` slug wasn't a live topic yet; it auto-minted as `organic` and is already live in relatedness — informational, not a problem to fix, but worth a glance: confirm it names a genuine new concept rather than a typo of an existing slug
  - `tombstoned_topics` — a `topics`/`mentions` slug names a topic a human tombstoned (vetoed); the link is stored but excluded from relatedness until it's deliberately re-minted via `publish_topic`
  - `unknown_category` — `category` isn't a live category slug; stored but inert until it's minted (`/kb` UI) or fixed
  - `links_update_failed` — the topic/item-link index itself failed to write (distinct from an unknown slug); re-publish (even unchanged) to repair
  - `intents_update_failed` — the file-intents index failed to write; re-publish (even unchanged) to repair
  - `consider_update` — a rev-1 doc mint shares primary topics with an existing active doc; check whether you should be updating that doc instead — see **Topics & categories**
  - `no_topics` — `topics` is empty on a doc/memory publish (entries are exempt); link 1-3 existing topics (see `gitian-kb://vocab`) or mint a genuine new concept
  - `project_name_topic` — a `topics`/`mentions` slug just repeats `project` or the repo basename; it adds near-zero relatedness signal (every item in the project/repo would carry it) — link a concept topic instead
  - `undescribed_topics_minted` — the subset of this publish's `organic_topics_minted` slugs whose topic still has no description; call `publish_topic` on each now while the context is fresh
- On `validation_failed`, fix every listed `issue` and retry — the error's `format_resource` field names the exact guide to re-read.
- `contention` on a successful `publish_doc` means another active doc declares overlapping `files` — read it (`get`), coordinate or narrow scope, and cross-link it in `related`. If the hit carries a non-null `owner` (a teammate's plan on a shared org repo, not your own), pass that login as `get`'s `owner` param — a bare `get slug` looks up *your own* item at that slug (or `not_found`), not theirs. `file_intents` hits carry the same `owner` field for the same reason.
- **Org-wide visibility** is read-time and repo-scoped: when a repo belongs to a gitian org you're seated + entitled in, `file_intents`/`contention` widen from your own rows to every currently-seated member's rows on that repo, and `get` with `owner` can read a teammate's doc under the grant its frontmatter declares (`repo` + `files`) — nothing here is a separate opt-in or a different tool.
- `body` is distilled content — decisions made, the rationale behind them, alternatives considered and rejected — not a transcript of the conversation or a chronological log of messages. Write what a future reader needs to understand and trust the outcome.
- Set `repo` to the working repository as `owner/name` (derive it from `git remote get-url origin`); explicit `null` when the work isn't repo-bound or there's no remote — never guess. The repo doesn't need to be connected to gitian; identity is late-binding.

## Writing bodies

Bodies are Obsidian-flavored intent documentation — why the thing is the way it is, not a transcript. Link related KB items inline with `[[slug]]` wikilinks (they resolve in the UI and add a direct, always-1.0 relatedness link between the two items — stronger than any topic overlap), structure with headings, and include short code snippets where they say it better than prose. Reference code where the knowledge lives: in a repo already instrumented with gitian docs (a `.gitian/` config directory, `@gitian` annotations, paired `docs/` files), point at those anchors — an annotation id, a doc path — instead of duplicating their content; in any other repo, reference files and symbols plainly. **Never add `@gitian` annotations or any gitian markup to a codebase that isn't already using the gitian docs system** — publishing to the KB never licenses editing code comments; in-code instrumentation is opt-in via the gitian-docs plugin only.

## Terminal-state discipline

Before telling the user work is done, abandoned, or paused, re-publish the governing doc with the updated `status` (and `impl_status`) — the platform stamps `updated_at` for you, so this is the only step you owe it. A landed feature gets **both** the status flip and a `recap` doc; shipping one without the other leaves the KB half-updated and misleads whoever reads it next.

Worked example: a feature branch merges. Re-publish the design/plan doc's slug — the full manifest again, with `status: landed`, `impl_status: done`, `commits: [<sha>]` updated — then `publish_doc` a new `type: recap` covering what shipped, the key decisions, gotchas discovered, and any deferred follow-ups.

## The journal is a running record (entries)

The day's entry is a running record of meaningful events, appended to as they happen — not a day's-end summary gated behind a whole-day bar. When something meaningful happens mid-session — a feature lands, a real blocker appears, a decision settles, a finding surfaces, or the conversation pivots off a thread — fold it in. One entry exists per scope per day.

**`append_entry` is the primary journaling verb** — the default way to add to today's entry, every time. It's a small, targeted call (`date`/`scope` optional, default today UTC/`work`) that creates the entry if none exists yet or appends `section` to the body if one does, union-merging `tags`/`topics`/`mentions`/`commits`/`related` along the way, without re-sending the whole body — and it's atomic against concurrent writers (two agents appending to the same day's entry both land, neither clobbers the other). Reach for `publish_entry` only for a genuine full rewrite of the day's entry — correcting or restructuring what's already there — not as the everyday path.

The bar for "meaningful" is what a teammate would care to hear at standup — a pivot, a diagnosis, a settled design all clear it; routine mechanical work (a rename, a re-run, a dependency bump) does not. The journal records the day a colleague would want to catch up on, not a command log.

## Never auto-publish

Publish at natural completion points or on explicit user intent, full stop. Never publish silently, never in bulk, and never mid-task on a timer — the meaningful-event bar and the discipline above only hold if every publish is a deliberate call, not a background habit.

Natural completion points: a design conversation converges, a plan is finished and about to be handed to implementation, a feature merges, a work session wraps up worth an entry, the conversation pivots off a thread, or a compaction has just squashed (or a manual `/compact` is about to squash) undistilled context. If none of those has happened, don't publish yet — keep working and revisit the trigger table later.

## The nudge layer

Underneath this skill, the plugin also runs a client-side nudge layer: hooks (`hooks/*.sh`, POSIX
sh delegating all JSON work to python3, no `jq`) passively harvest MCP traffic into a local
observation cache (`~/.claude/gitian-kb/state.json`, overridable via `GITIAN_KB_STATE_FILE`) and
use it to fire advisory nudges reinforcing the discipline above. This is local scaffolding, not
server enforcement: every nudge fires at most once per session (an epoch reset on `/clear` re-arms
them), every hook is fail-open (bad input, corrupt state, a missing `python3` — anything — falls
through to silence, never a partial nudge, never a non-zero exit), and a session that follows this
skill from the start is **silent when compliant** — zero nudge output, by design. That silence is
the release invariant, not an absence of coverage.

The eight nudges:

1. **Session-start vocab digest** (SessionStart) — on startup/clear/compact, a short digest of the
   cached vocab (topic count, any undescribed organic topics) when the cache already has one; on
   resume, zero/one/two lines noting a moved vocab revision and/or a stale (>12h) session record.
   Silent whenever there's nothing worth reporting.
2. **Orientation check** (PreToolUse on `Edit`/`Write`/`NotebookEdit`) — denies once, advisory, if
   this session's first file mutation happens with zero gitian KB reads recorded yet: a reminder to
   `file_intents`/`search`/`neighbors` before touching paths a plan elsewhere may already claim.
   States explicitly that the denial is advisory and re-sending the identical call passes through
   untouched.
3. **Publish lint** (PreToolUse on `publish_doc`/`publish_memory`/`publish_entry`/`append_entry`) —
   a client-side echo of the server's own lint (empty `topics`, a project/repo-name topic, a
   near-miss slug against the cached vocab) fired before the call ever reaches the server. The hook
   hashes the intercepted call, so an identical re-send passes untouched; `append_entry` is exempt
   from the empty-topics check on append, same as the server (linted only on create).
4. **Commit-nudge** (PostToolUse on `Bash`) — once per session, if a real commit (or `gh pr merge`)
   lands with no `append_entry`/journal activity in the last 2 hours, an advisory reminder to
   journal it. Silent whenever that 2h damper is already satisfied.
5. **Stop publish-reminder** (Stop) — once per session, blocks-with-reason if this turn crossed the
   "substantial work" line (≥3 `Edit`/`Write` or ≥1 commit in the transcript) with zero gitian
   publish/append calls anywhere. Always silent on a resumed `stop_hook_active` pass (loop guard) or
   whenever something was actually published.
6. **Mint follow-up** (PostToolUse, riding the same harvest pass as vocab caching) — the first time
   a session sees a given auto-minted (organic, undescribed) topic slug in a response's
   `organic_topics_minted` warning, one line naming it and pointing at an immediate `publish_topic`
   call; silent on every later repeat of a slug already prompted this session.
7. **Server warnings** — `no_topics`, `project_name_topic`, and `undescribed_topics_minted` (see
   **Publishing rules** below) are advisory, never rejections — the client-side lint (nudge 3)
   usually catches the same conditions earlier, before the round trip even happens.
8. **`/gitian-kb:status`** — run any time to inspect the cache directly: per-server vocab
   revision/age/topic count/undescribed topics, last publish/append times, the current session's
   counters, and which once-per-session flags have already fired this epoch. Read-only, never
   modifies state.

None of this should surprise an agent mid-session: a nudge names itself as advisory, says what to
do next, and — except for the deny-once orientation check and the block-once stop reminder, both of
which say so — never stops you from proceeding.
