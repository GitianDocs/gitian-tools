# gitian KB authoring discipline

This is the full discipline for **writing** to the gitian Knowledge Base: picking a primitive,
publishing a full manifest, revising an item safely, and the shape a body takes. It used to be the
body of `SKILL.md`; it moved here when writing moved to the `kb-scribe` subagent. Nothing was
dropped in the move.

**Who reads this file.** `kb-scribe` (it carries the core rules in its own prompt and reads this
for the long tail), and any agent that must publish inline because it cannot dispatch a subagent —
a subagent cannot spawn a subagent, so an implementer agent asked to publish reads this file and
does the work itself. The primary decides *when* to publish and *what* mattered; everything below
is *how*.

Companion references in this directory:

- `kb-targeting.md` — the `kb` argument, linked KBs, org KBs, `vocab_rev` freshness
- `topics.md` — topics, mentions, categories, the topic extraction contract
- `spec-authoring.md` — spec/plan/design/handoff/recap authoring: manifest derivation, commits
  list, the implementation-recap checklist

## Picking a primitive

Pick by shape, not size:

- **memory** — one atomic, durable fact: a preference, a gotcha, a project fact. Small and stable,
  even if the investigation that produced it took days.
- **doc** — a long-form artifact with a lifecycle: `spec`, `plan`, `design`, `handoff`, or `recap`,
  carrying the full manifest (status, impl_status, next_steps). Use it once the work has shape and
  needs tracking over time.
- **entry** — a dated journal record for a scope (`work` or `personal`): what happened, worth
  recalling later, even if it's short.

A multi-day effort with status and next steps is a doc even if the write-up is one paragraph; a
single durable fact is a memory even if it took a long investigation to learn.

## The loop

1. **Orient** — before any substantive work, not just before writing: read `gitian-kb://vocab`
   first (the live topic + category vocabulary — slug, description, degree for topics; slug, name,
   routing prompt for categories), then `search` (or `list`) the KB for the topic, then call
   `neighbors` on the best hit. This is RAG at work-start, not a publish-time formality — the item
   you're about to create may already exist under a different slug you haven't thought of, the
   vocab may already have a topic naming what you're about to link, and `neighbors`' topic-derived
   neighborhood surfaces adjacent decisions a keyword search alone would miss. Working in a repo?
   `file_intents` the repo too — it lists which in-flight docs claim which paths; on overlap with
   what you're about to touch, `get` the contending doc before proceeding. `include_landed: true`
   widens that same call into "who has reworked this area before" — deactivated (landed/abandoned)
   plans included, not just what's in flight.
2. **Thread** — read the matching `gitian-kb://format/<primitive>` resource, then adopt what's
   already decided: don't re-litigate a settled design, cross-link into it via `related` (slugs)
   instead of duplicating it. Before heavily editing a doc you didn't write, pull it with `get` and
   skim `history` to see how it evolved. Call `neighbors` on it too — its topic-derived `why`
   surfaces adjacent decisions you wouldn't have thought to `search` for.
3. **Publish** — call the right tool (`publish_memory` / `publish_doc` / `publish_entry`) with the
   full manifest, not a partial one, `topics`/`mentions`/`category` included (see `topics.md`).
   Updating today's journal entry is the one exception: use `append_entry` (see **The journal is a
   running record** below) — a small, targeted call, not a full-manifest publish. Revising an
   existing item is `patch_doc`/`patch_memory`, never a re-publish (see **Revising an existing
   item** below).
4. **Confirm** — every `publish_*` call returns a `url`; surface it: "published → `<url>`".
   (`retract_item` returns `{slug, rev, tombstoned}` — no url.)

Retract obsolete items with `retract_item` rather than trying to delete their content — it appends
a tombstone revision. History survives, and re-publishing the same slug un-deletes it.

A `publish_*` result may also carry `suggested_topics`: up to 5 existing topics the server noticed
read close to what you just wrote but that you didn't already link in `topics`/`mentions`. Review
it on every publish — when a suggestion is genuinely on-topic, add it (a `patch_doc` on `topics` is
enough) rather than ignoring it; precise linking is what keeps `neighbors` useful for everyone who
calls it after you.

Worked example: fixing a flaky CI failure. `search "flaky auth"` turns up nothing; read
`gitian-kb://format/memory`; fix the bug; `publish_memory` with slug `ci-flaky-oauth-token`,
`type: reference`; report "published → `<url>`" from the response.

## When to publish — trigger table

The primary owns this decision; it is reproduced here for the inline path.

| Trigger | Tool | Notes |
|---|---|---|
| Design settled | `publish_doc` | `type: spec` or `type: design` |
| Plan written | `publish_doc` | `type: plan` |
| Durable fact learned | `publish_memory` | a preference, gotcha, or project fact worth recalling next session |
| Meaningful event (pivots included) | `append_entry` | append to today's running entry as it happens — see the running-record section below; `publish_entry` remains for a full rewrite |
| Handing off mid-stream | `publish_doc` | `type: handoff` |
| Conversation pivots off a thread | `publish_doc`/`publish_memory` + `append_entry` | a pivot ends the old thread as surely as finishing it — publish or update its governing item *before* engaging the new topic, and fold the pivot into today's journal entry |
| Context compacted | `publish_doc` | `type: handoff` — right after a compaction, distill the summary plus what is still held into a handoff a fresh agent could resume from (the session hook reminds you); before a *manual* `/compact`, run `/gitian-kb:handoff` to capture state pre-squash |
| Feature landed | `publish_doc` (`type: recap`) **and** flip the feature doc's `status` to a terminal value | do both — a recap without the status flip leaves the KB stale |

## Who does what

| Operation | Who | Why |
|---|---|---|
| Deciding a publish is warranted, and what mattered | Primary, always | Judgment; it is the only thing that needs the session's full context |
| Rev-1 authorship + its publish call | `kb-scribe` | Authored from the brief (+ transcript), never retyped — the primary's words don't have to be re-emitted on the expensive model |
| Mechanical doc revisions (flips, merges) | `kb-scribe` via `patch_doc` / `patch_memory` | Server-side merge — the body never re-crosses the wire, so the saving is real and the doc can't be truncated |
| Mid-body edits (ticking a task, correcting a line) | `kb-scribe` via `body_edits` | The server applies the delta; no model retypes a body |
| Journal appends | `kb-scribe` via `append_entry` | Highest frequency; clobber-proof; all clients |
| Orientation sweep, vocab refresh | `kb-librarian` | Read-heavy, mechanical, context-fat — and read-only by construction |
| Merging a duplicate into a survivor the primary named | `kb-scribe`'s dedupe run | Mechanical once the survivor is chosen: verbatim `body_append`, union-merged lists, mutual `related`, `retract_item` of the duplicate |
| Which duplicate survives | Primary, always | Judgment about which body and manifest the KB carries forward |
| A single one-off `search`/`get` | Inline | Spawning a subagent costs more than the call it would save |

**No model ever retypes a body verbatim.** Writes are either *authored* (the scribe's own words,
from brief + transcript) or *patched* (the server applies the delta). The one surviving exception
is the dedupe run's verbatim `body_append`, which keeps its 20,000-character ceiling.

## Orient first — the resources

Read `gitian-kb://vocab` (see `topics.md`) plus the matching format resource before the *first* use
of each publish tool in a session — don't guess the shape:

- `gitian-kb://vocab` — the live topic vocabulary and category routing prompts, as JSON
- `gitian-kb://format/overview` — the three primitives, upsert/versioning rules, null-not-omitted
- `gitian-kb://format/memory` — memory fields and slug conventions
- `gitian-kb://format/doc` — the full doc manifest, enums, distillation and recap guidance
- `gitian-kb://format/entry` — the journal format and the running-record discipline

**If your client cannot read MCP resources, call the `read_resource` tool** (`{ uri, kb? }`) — it
returns exactly what the resource read returns. A subagent has no resource-read tool at all, so
this is the scribe's and the librarian's only path to the vocab and the format docs.

`search` before inventing a new slug. The same subject may already have an item under a name you
didn't guess — re-publish the *same* slug to update it (appends a revision); only mint a new slug
for a genuinely new subject.

## Schema authority

Installed tool schemas are a cached snapshot, not the live contract — they can lag behind the
server (a past cache once omitted `summary`/`repo` entirely, so every publish built strictly from
the cached schema failed `validation_failed`). The live `gitian-kb://format/*` resources and the
server's own validation errors are always authoritative. If `validation_failed` names a field the
cached tool schema didn't mention, that isn't a bug in your call — trust the server, add the
field, and retry.

## Revising an existing item: read, then write

A revision is a **read → edit → write** loop, and the read is not optional: it is where you learn
both the item's current fields and the `rev` your write has to build on.

### Prove what you read: `base_rev`

Two agents can hold the same item at once, and the loser of that race used to overwrite the winner
silently. So every write that can *revise* an existing item — `publish_memory` / `publish_doc` /
`publish_entry`, `patch_doc` / `patch_memory`, and `retract_item` — takes `base_rev`: **the `rev`
you read before making your change**, from the `get` you just did (or from the response of your own
last write, which reports the rev it landed). It never appears in frontmatter and never moves
`body_hash`; an unchanged re-publish with the right `base_rev` still returns `unchanged: true`.
`append_entry` is the one exemption — it accepts no `base_rev` at all, because its merge is
union-based and concurrent appends both land.

- **Minting?** Omit it, or pass `base_rev: 0` to assert "this must be a create" — against a slug
  that already exists, `0` is a `rev_conflict` rather than a silent revision.
- **Revising?** Pass the rev you actually read on this call. Never a number remembered from earlier
  in the session, carried over from another item, or incremented by hand.
- **`base_rev_required`** — the slug already exists and your write didn't say what it was built on.
  It deliberately carries no `head_rev`: the remedy is the read, not the number. `get` the slug
  (`include_body: false` is enough), then retry with the `rev` it returned.
- **`rev_conflict`** — someone landed a revision after your read. It carries `head_rev` (the number
  to build on now), `head.author_login` (who moved it), `frontmatter_changed` (which fields they
  touched) and `body_diff` (a unified diff of the body change) — read those first, because a small
  delta usually needs no second round trip. When they aren't enough — `body_diff` is `null` with an
  `omitted_reason` of `diff_too_large` or `base_rev_not_found` — `get` the item again. Then
  **re-apply YOUR change on top of theirs** and retry with `base_rev: <head_rev>`. Never resubmit
  the identical payload with the new number: that is exactly the lost update the guard exists to
  prevent, and it deletes whatever they just wrote.
- A conflict on a doc you didn't write, in an org KB you can write but not read, comes back
  stripped — `head`, `frontmatter_changed` and `body_diff` all `null`, with
  `omitted_reason: "read_requires_entitlement"` — while `head_rev` survives so you can still
  proceed. Re-reading is no help here: a `get` on that item is refused for the same reason. Say
  plainly that the other revision isn't visible rather than guessing at it.

### Patch, don't re-publish

`publish_*` is a full PUT — it requires the entire `body` on every call. That makes it the wrong
tool for a revision, and not merely a wasteful one: re-emitting a long body is how bodies get
truncated. Use the patch tools instead.

- **`patch_doc` / `patch_memory`** — `slug` plus only the fields you're changing. Omitted field =
  unchanged; supplied field = **replaces wholesale**. There is no `body` field on these tools: the
  server keeps the existing body byte-identical unless you send `body_edits` or `body_append`. Use
  them for status flips, adding `commits`, updating `next_steps`, correcting `files` — every
  revision that isn't a rewrite.
- **`body_edits`** is how a patch changes text *inside* the body, with the exact-string semantics
  of an editor: an array (1–50) of `{ old_string, new_string, replace_all? }`, applied in order
  against the head body, each edit seeing the previous edit's output. Without `replace_all`,
  `old_string` must match **exactly once**; with it, at least once. The patch is **atomic** — a
  miss rejects the whole call, `edit_no_match` or `edit_ambiguous` naming the failing `edit_index`,
  and nothing is written and no revision is created. `old_string === new_string` is
  `validation_failed`. The response adds `edits_applied` (the number of edits, not of
  replacements). Ticking a task in a 40,000-character plan is two dozen characters over the wire,
  and truncation is not expressible: deleting text requires quoting it, which is why `body_edits`
  is exempt from the shrink *reject* tier (the `body_shrank` advisory still fires).
  Quote enough surrounding text to be unique, and prefer several small edits over one large one.
- **`body_append`** only ever grows the body (blank-line separated), and applies *after* any
  `body_edits` in the same call. An implementation recap appended to a landed doc is exactly this.
- **Need a list's current contents first?** Replace-wholesale means appending to `related` or
  `commits` requires knowing what's already there. Read it with `get` and `include_body: false` —
  a manifest-only read that skips the body entirely. Never rebuild a list from memory.
- **Entries have no patch tool** — `append_entry` already is one. Omit its `section` to revise
  only frontmatter and leave the body untouched.
- **Patch never creates.** An unknown or tombstoned slug returns `not_found`; use `publish_*` to
  mint or to resurrect.
- Warnings still apply, computed against the *merged* manifest — patching `status` to `landed`
  with no commits still raises `landed_without_commits`.

Reserve `publish_doc` / `publish_memory` for rev-1 authorship and genuine full rewrites. If a
rewrite cuts the body by more than a quarter, the server rejects it with `body_shrank` until you
re-send with `body_replaced: true` — that guard exists because a truncated body looks exactly
like a deliberate edit, and it has already destroyed a doc once.

Every write returns `body_length` and `body_hash` (sha256 of the body alone, so frontmatter edits
never move it). Quote those to confirm a write landed intact — never assert a byte count or an
"identical" claim you didn't actually compute.

## Publishing rules

- Every schema key must be present in the call — explicit `null` (or `[]` for list fields) when a
  value is genuinely unknown, never omit the key. A thin publish that drops required keys is
  rejected.
- Never write `created_at`, `updated_at`, `rev`, or `author` yourself — the platform stamps these
  from the token and the revision; they aren't yours to set.
- Slugs are stable, lowercase-kebab, and name the thing (`auth-token-nullable`, not `note-1`).
  Entries are the exception: they take `date` + `scope`, never a slug — the platform derives one
  from both.
- Slugs share one namespace per KB across all three primitives — a `memory` and a `doc` can't reuse
  the same slug. Pick something specific enough not to collide, and `search` first so you don't
  collide silently.
- An identical re-publish returns `unchanged: true`. That is success, not an error — don't retry it
  or treat it as a failure.
- **Populate frontmatter — don't default to null.** `project`, `repo`, and `tags` must be filled
  whenever they're derivable, not left null out of habit. The session context (repo, branch, date)
  and the brief give you what you need for `repo`; set `project` from the obvious repo/workspace
  name. Explicit `null` is only for work that's genuinely not project- or repo-bound — never a
  shortcut. Always include `summary`, especially on memories, where it's the only preview a list
  view shows.
- **Every successful write answers `landed_in`** — the label of the KB the item is actually in
  (`home`, `<org>/team`, `login/kb-slug`). Read it on every write and report it; never assume the
  write landed in the KB you aimed at. When it isn't the KB you meant, say so — a landing is never
  fixed by republishing the same body into another KB.
- `warnings` on a successful publish are advice to act on, not blockers. Twenty-two codes:
  - `no_tags` — no tags supplied; add 1-3 to aid retrieval
  - `no_project` — `project` is null; derive it from context or confirm this isn't project-bound
  - `no_repo` — `repo` is null; derive it from `git remote get-url origin` (the session context
    already surfaces this) or confirm the work isn't repo-bound
  - `landed_without_commits` — `status: landed` but `commits` is empty; add the landing commit(s)/PR
  - `impl_done_status_open` — `impl_status: done` but `status` is still draft/designing/in-progress/blocked; reconcile before closing out
  - `terminal_with_next_steps` — `status` is terminal but `next_steps` is non-empty; confirm they still apply
  - `plan_without_files` — an active plan with a `repo` but empty `files`; declare the paths the plan will touch (trailing `/` = subtree) so parallel agents can detect contention
  - `organic_topics_minted` — a `topics`/`mentions` slug wasn't a live topic yet; it auto-minted as an undescribed stub and is already live in relatedness — informational, not a problem to fix, but worth a glance: confirm it names a genuine new concept rather than a typo of an existing slug
  - `tombstoned_topics` — a `topics`/`mentions` slug names a topic a human tombstoned (vetoed); the link is stored but excluded from relatedness until it's deliberately re-minted via `publish_topic`
  - `unknown_category` — `category` isn't a live category slug; stored but inert until it's minted (`/kb` UI) or fixed
  - `links_update_failed` — the topic/item-link index itself failed to write (distinct from an unknown slug); re-publish (even unchanged) to repair
  - `intents_update_failed` — the file-intents index failed to write; re-publish (even unchanged) to repair
  - `org_kb_available` — the item stayed in your `home` KB, but its `repo` belongs to a gitian **org whose `team` KB you can publish into**, and nothing was rerouted: either routing is switched off for you, or this primitive never routes (a memory, or an entry the routing didn't claim). Personal-KB items about an org repo are invisible to teammates (see `kb-targeting.md`). If this is team work, re-publish/append it with `kb` set to the `<org>/team` address the note names; if it's genuinely personal, ignore the warning. **An explicit `kb` silences it** — both routing advisories (this one and `slug_exists_in_other_kb`) are about **kb-less** landings only: if you said where the item goes, the server has no better guess to offer
  - `repo_missing_cannot_route` — a kb-less **and** repo-less `publish_doc`/`publish_entry`/`append_entry` CREATED the item in `home` while you belong to an org the write could have routed to. Nothing routed because there was nothing to route on: routing is computed from `repo` alone — **no `repo`, no routing** — so the landing is `home` by default rather than by decision. Report it and let the primary choose the target: `kb` set to the `<org>/team` address, or `repo` as the normalized `owner/name` so routing can do it
  - `slug_exists_in_other_kb` — a create with no `kb` landed in `home` while that same slug already exists in another KB you can write — usually the org's `team` KB, i.e. the work you meant to revise is over there. Nothing is broken and nothing was overwritten, but you have probably just forked it: report the warning and let the primary decide between revising the existing item (with `kb` + `base_rev`) and keeping the new one
  - `kb_read_paywalled` — the doc or entry was ROUTED into an org `team` KB you can write but not read, because the org has no live subscription. The write landed and stays readable to you — you wrote it; a `get` on a *teammate's* item there answers `read_requires_entitlement` until the org subscribes. Nothing to fix — the response's `routed_to`/`landed_in` names where it went
  - `consider_update` — a rev-1 doc mint shares primary topics with an existing active doc; check whether you should be updating that doc instead — see `topics.md`
  - `no_topics` — `topics` is empty on a doc/memory publish (entries are exempt); link 1-3 existing topics (see `gitian-kb://vocab`) or mint a genuine new concept
  - `doc_without_topics` — a `publish_doc` landed with no `topics`/`mentions` at all and the owner isn't on server-side extraction; apply the topic extraction contract in `topics.md` and re-publish
  - `project_name_topic` — a `topics`/`mentions` slug just repeats `project` or the repo basename; it adds near-zero relatedness signal (every item in the project/repo would carry it) — link a concept topic instead
  - `undescribed_topics_minted` — the subset of this publish's `organic_topics_minted` slugs whose topic still has no description; call `publish_topic` on each now while the context is fresh
  - `body_shrank` — the body you sent is more than 10% shorter than the stored one. Treat this as a truncation alarm, not a formality: compare `body_hash` in the response against what you expected, and if you didn't mean to cut the body, re-read the head revision and republish it in full. Past 25% (and more than 2000 characters) the publish is **rejected** outright with a `body_shrank` error instead — acknowledge a deliberate rewrite with `body_replaced: true`, or avoid the whole problem by using `patch_doc`/`patch_memory`, which never send a whole body at all
- **Report every `warnings` entry verbatim** — code, path and note as the server phrased it. Don't
  summarize a warning away, don't decide one doesn't matter, and don't silently "handle" one (e.g.
  re-publishing to retry a `links_update_failed`) unless you were told to.
- On `validation_failed`, fix every listed `issue` and retry — the error's `format_resource` field
  names the exact guide to re-read.
- On `base_rev_required`, don't re-send the same call: the slug already exists and your write never
  said which revision it was built on. `get` it (`include_body: false` is enough) and retry with
  `base_rev` set to the `rev` you read — see **Revising an existing item** above.
- On `rev_conflict`, re-apply your change on top of the new head — reading `head_rev`,
  `head.author_login`, `frontmatter_changed` and `body_diff` off the error, re-reading the item when
  those aren't enough — then retry with `base_rev: <head_rev>`. Never resubmit the same payload with
  the new number; that silently deletes the revision you collided with.
- `contention` on a successful `publish_doc` means another active doc declares overlapping `files`
  — read it (`get`), coordinate or narrow scope, and cross-link it in `related`. Contention is
  scanned within the KB you published into: teammates contend with each other because they publish
  into the same **org KB**, not because a read reached across owners.
- `body` is distilled content — decisions made, the rationale behind them, alternatives considered
  and rejected — not a transcript of the conversation or a chronological log of messages. Write
  what a future reader needs to understand and trust the outcome.
- Set `repo` to the working repository as `owner/name` (derive it from `git remote get-url origin`);
  explicit `null` when the work isn't repo-bound or there's no remote — never guess. The repo
  doesn't need to be connected to gitian; identity is late-binding.
- **Never report a verification you did not run.** State only what a tool actually returned. Do not
  assert byte counts, character counts, hashes, or "identical"/"verified" unless you executed the
  comparison and are quoting its output. An honest "not verified" is always acceptable; a fabricated
  confirmation is never. *This rule exists because a runner once reported "Read: 62,698 characters /
  Published: 62,698 characters / Byte-for-byte identical ✓" while actually publishing a body
  truncated by 30.6%. The false report is what let the corruption reach the KB unnoticed.*

## Writing bodies

Bodies are Obsidian-flavored intent documentation — why the thing is the way it is, not a
transcript. Link related KB items inline with `[[slug]]` wikilinks (they resolve in the UI and add
a direct, always-1.0 relatedness link between the two items — stronger than any topic overlap),
structure with headings, and include short code snippets where they say it better than prose. A
wikilink can also cross KBs: `[[kb-slug/item-slug]]` is resolved relative to the reader (your own
KB of that name wins; ambiguous across two linked KBs renders as a dangling link rather than a
guess) and `[[login/kb-slug/item-slug]]` names a linked KB's item exactly — prefer the qualified
three-segment form when writing about a linked KB's item, for the same reason you pass a qualified
`kb` back verbatim. Reference code where the knowledge lives: in a repo already instrumented with
gitian docs (a `.gitian/` config directory, `@gitian` annotations, paired `docs/` files), point at
those anchors — an annotation id, a doc path — instead of duplicating their content; in any other
repo, reference files and symbols plainly. **Never add `@gitian` annotations or any gitian markup
to a codebase that isn't already using the gitian docs system** — publishing to the KB never
licenses editing code comments; in-code instrumentation is opt-in via the gitian-docs plugin only.

Authoring from a transcript: distill, never transcribe. The body is the decisions, the rationale
and the rejected alternatives — not the conversation that produced them. **Never quote a
credential, token, key or secret seen in a transcript**, not even redacted-looking fragments; if a
decision turns on one, name the variable, never the value.

## Terminal-state discipline

Before work is reported done, abandoned, or paused, the governing doc gets the updated `status`
(and `impl_status`) — a `patch_doc` is the whole job, and the platform stamps `updated_at` for you.
A landed feature gets **both** the status flip and a `recap` (a `## Implementation recap` section
appended to the canonical doc, or a separate `type: recap` doc — see `spec-authoring.md`); shipping
one without the other leaves the KB half-updated and misleads whoever reads it next.

A terminal-state flip or a retraction is only done when the brief says so explicitly. Inferring one
from "we're done here" is exactly the judgment call to hand back instead.

Worked example: a feature branch merges. `patch_doc` the design/plan doc's slug with
`status: landed`, `impl_status: done`, `commits: [<sha>]` and `base_rev` from the `get` you just
made — then append the recap with `body_append` on that same call, or `publish_doc` a new
`type: recap` doc covering what shipped, the key decisions, gotchas discovered, and any deferred
follow-ups.

## The journal is a running record (entries)

The day's entry is a running record of meaningful events, appended to as they happen — not a day's
end summary gated behind a whole-day bar. When something meaningful happens mid-session — a feature
lands, a real blocker appears, a decision settles, a finding surfaces, or the conversation pivots
off a thread — fold it in. One entry exists per scope per day.

**`append_entry` is the primary journaling verb** — the default way to add to today's entry, every
time. It's a small, targeted call (`date`/`scope` optional, default today UTC/`work`) that creates
the entry if none exists yet or appends `section` to the body if one does, union-merging
`tags`/`topics`/`mentions`/`commits`/`related` along the way, without re-sending the whole body —
and it's atomic against concurrent writers (two agents appending to the same day's entry both land,
neither clobbers the other). Reach for `publish_entry` only for a genuine full rewrite of the day's
entry — correcting or restructuring what's already there — not as the everyday path.

The bar for "meaningful" is what a teammate would care to hear at standup — a pivot, a diagnosis, a
settled design all clear it; routine mechanical work (a rename, a re-run, a dependency bump) does
not. The journal records the day a colleague would want to catch up on, not a command log.

## Never auto-publish

Publish at natural completion points or on explicit user intent, full stop. Never publish silently,
never in bulk, and never mid-task on a timer — the meaningful-event bar and the discipline above
only hold if every publish is a deliberate call, not a background habit. **That decision belongs to
the primary**: a scribe publishes what a brief asks for and nothing more, and an agent working
inline applies the trigger table above rather than publishing on a hunch.

Natural completion points: a design conversation converges, a plan is finished and about to be
handed to implementation, a feature merges, a work session wraps up worth an entry, the conversation
pivots off a thread, or a compaction has just squashed (or a manual `/compact` is about to squash)
undistilled context. If none of those has happened, don't publish yet.

## Dedupe: merging a duplicate into a survivor

The primary names a **survivor** slug and a **duplicate** slug (both docs, and the ranking is
theirs). Merge one into the other, mechanically:

1. `get` both. The survivor manifest-only (`include_body: false`) — you need its list fields and
   its `rev`, not its body. The duplicate WITH its body, because you are about to move that body
   verbatim.
2. `patch_doc` the survivor with `body_append` = a `## Merged from <slug>` heading naming the
   duplicate, followed by the duplicate's body **copied exactly, byte for byte** — no summarizing,
   no re-heading, no tidying. **Ceiling: 20,000 characters.** A duplicate body longer than that is
   not something to retype at all — stop and hand the merge back, naming the length of the body you
   actually received, and only if you counted it yourself, since `get` returns no `body_length`
   (that comes back on WRITES, not reads).
3. In that same `patch_doc`, replace the survivor's list fields with the **union** of both
   manifests' values — `files`, `tags`, `topics`, `mentions`, `related`, `commits`, `next_steps`,
   `blockers` — built from what the two `get`s actually returned, never from memory, and preserving
   the survivor's order with the duplicate's new entries appended. Union means nothing is dropped;
   you are not choosing between values.
4. Cross-link both ways: the duplicate's slug goes into the survivor's `related` (part of step 3's
   union), and the survivor's slug goes into the duplicate's `related` via its own `patch_doc`, so
   the tombstone still points at where the content went.
5. `retract_item` the duplicate.

Every one of those writes carries `base_rev` from the read that immediately preceded it — the
survivor's `patch_doc` and the duplicate's cross-link `patch_doc` from their step-1 `get`s, and
step 5's `retract_item` from the `rev` that step 4's `patch_doc` returned (step 4 moved the
duplicate's head, so its step-1 rev is stale by then). If a write conflicts anyway, the
re-read/re-apply/retry rule above applies unchanged.

**Never pick the survivor.** Which of two duplicate docs keeps its slug is a judgment about which
body and which manifest the KB should carry forward — the primary's call, exactly like topic
choice. Asked to *propose* candidates instead, that is a read-only job for `kb-librarian`: it
reports pairs and stops.

## The nudge layer

Underneath this discipline, the plugin runs a client-side nudge layer: hooks (`hooks/*.sh`, POSIX
sh delegating all JSON work to python3, no `jq`) passively harvest MCP traffic into a local
observation cache (`~/.claude/gitian-kb/state.json`, overridable via `GITIAN_KB_STATE_FILE`) and
use it to fire advisory nudges reinforcing the rules above. This is local scaffolding, not server
enforcement: every nudge fires at most once per session (an epoch reset on `/clear` re-arms them),
every hook is fail-open (bad input, corrupt state, a missing `python3` — anything — falls through
to silence, never a partial nudge, never a non-zero exit), and a session that follows this
discipline from the start is **silent when compliant** — zero nudge output, by design. That silence
is the release invariant, not an absence of coverage. Subagent MCP traffic is harvested under the
parent session's id, so a brief the primary dispatched satisfies the counters exactly as an inline
call would.

The nine nudges:

1. **Session-start context** (SessionStart) — on startup/clear/compact, the delegation directive
   plus the derived repo/branch/date and transcript lines; on resume, zero/one/two lines noting a
   moved vocab revision and/or a stale (>12h) session record. Silent whenever there's nothing worth
   reporting.
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
4. **Routing guard** (PreToolUse on `publish_doc`/`publish_entry`/`append_entry`) — stateless and
   deterministic, not once-per-session: it denies a write passing **neither `kb` nor `repo`** (such
   a write cannot route, so it lands in `home`) and names the `repo` to set; an explicit `kb` —
   `"home"` for personal work — satisfies it too. Silent for memories, `patch_*`/`retract_*`, and
   for any write already carrying one of the two fields.
5. **Commit-nudge** (PostToolUse on `Bash`) — once per session, if a real commit (or `gh pr merge`)
   lands with no `append_entry`/journal activity in the last 2 hours, an advisory reminder to
   journal it. Silent whenever that 2h damper is already satisfied.
6. **Stop publish-reminder** (Stop) — once per session, blocks-with-reason if this turn crossed the
   "substantial work" line (≥3 `Edit`/`Write` or ≥1 commit in the transcript) with zero gitian
   publish/append calls anywhere. Always silent on a resumed `stop_hook_active` pass (loop guard) or
   whenever something was actually published.
7. **Mint follow-up** (PostToolUse, riding the same harvest pass as vocab caching) — the first time
   a session sees a given auto-minted, undescribed topic slug in a response's
   `organic_topics_minted` warning, one line naming it and pointing at an immediate `publish_topic`
   call; silent on every later repeat of a slug already prompted this session.
8. **Server warnings** — `no_topics`, `project_name_topic`, and `undescribed_topics_minted` (see
   **Publishing rules** above) are advisory, never rejections — the client-side lint (nudge 3)
   usually catches the same conditions earlier, before the round trip even happens.
9. **`/gitian-kb:status`** — run any time to inspect the cache directly: per-server vocab
   revision/age/topic count/undescribed topics, last publish/append times, the current session's
   counters, and which once-per-session flags have already fired this epoch. Read-only, never
   modifies state.

None of this should surprise an agent mid-session: a nudge names itself as advisory, says what to
do next, and — except for the deny-once orientation check and the block-once stop reminder, both of
which say so — never stops you from proceeding.

## Companion plugins

- **gitian-spec** — when installed, a *request* to write a spec, plan, design, brainstorm, handoff
  or session note routes through its skill: it scans the KB, briefs the scribe, and reads the result
  back. The authoring rules for that class of doc are in `spec-authoring.md` here.
- **gitian-docs** — keeps `@gitian` annotations and paired `docs/` files in sync when code changes.
  It is the only path by which in-code instrumentation is ever added; KB authoring never is.
