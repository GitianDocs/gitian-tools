# Scribe and librarian playbook

The procedures `kb-scribe` and `kb-librarian` need on a few runs, not on every run. Each agent's own
prompt carries everything an ordinary dispatch needs; read this file **one section at a time**: Grep
it for `^## `, then Read from the section's line to the next heading. The sections are written to
stand alone.

Scribe sections: **Retraction**, **Dedupe run**, **Bulk writes**, **Topics and categories**,
**Terminal flips and the journal**, **Routing edge cases**, **Reports and follow-ups**. Librarian
sections start with `Librarian:`.

## Retraction

`retract_item`, only when the brief says so explicitly (never inferred from "we're done here"; an
implicit one is a `NEEDS SIGN-OFF`). It carries `base_rev` from the `get` you just did.

When the brief names what replaces the item (a merge), pass `redirect_to: "<survivor slug>"` (same
KB): the old slug then resolves to the survivor on reads, every `[[old]]` and `related` link to it
keeps working, the response carries `redirected_to`, and there is nothing to repoint.

Without a replacement, the response's `referrers` (and a `dangling_referrers` warning) names every
live item still linking to the retracted slug. Report that list as open follow-up rather than
guessing a replacement or deleting the links. Never invent a `redirect_to` the brief did not name.

A retraction is never batchable (`unsupported_tool` inside `batch_write`): it is a decision with its
own `base_rev` chain.

## Dedupe run

The brief names a **survivor** slug and a **duplicate** slug (both docs; the ranking is the
primary's, and you never pick it):

1. `get` both. The survivor manifest-only (`include_body: false`): you need its list fields and its
   `rev`. The duplicate WITH its body, because you are about to move that body verbatim.
2. `patch_doc` the survivor with `body_append` = a `## Merged from <slug>` heading naming the
   duplicate, followed by the duplicate's body **copied exactly, byte for byte**: no summarizing,
   no re-heading, no tidying. **Ceiling: 20,000 characters.** A longer body is not something to
   retype at all: stop and hand the merge back, naming the length of the body you actually
   received, and only if you counted it yourself, since `get` returns no `body_length`
   (`body_length` comes back on WRITES, not reads).
3. In that same `patch_doc`, replace the survivor's list fields with the **union** of both
   manifests' values (`files`, `tags`, `topics`, `mentions`, `related`, `commits`, `next_steps`,
   `blockers`), built from what the two `get`s actually returned, never from memory, preserving the
   survivor's order with the duplicate's new entries appended. Union means nothing is dropped.
4. Cross-link both ways: the duplicate's slug into the survivor's `related` (part of step 3's
   union), and the survivor's slug into the duplicate's `related` via its own `patch_doc`, so the
   tombstone still points at where the content went.
5. `retract_item { slug: <duplicate>, redirect_to: <survivor>, base_rev }`. The redirect IS the
   repair for every referrer: the duplicate's slug now resolves to the survivor, so each
   `[[<duplicate>]]` wikilink and `related: [<duplicate>]` entry keeps landing and none of them is
   repointed. Confirm the response carries `redirected_to: "<survivor>"` (the proof that the
   tombstone and the redirect both landed) and no `dangling_referrers` warning.

Every one of those writes carries `base_rev` from the read that immediately preceded it: the two
`patch_doc`s from their step-1 `get`s, and step 5's `retract_item` from the `rev` step 4's
`patch_doc` returned (step 4 moved the duplicate's head, so its step-1 rev is stale by then). A
conflict follows the scribe's `rev_conflict` rule unchanged.

Report both slugs in survivor-then-duplicate order, the survivor's new `rev` with the
`body_length`/`body_hash` the write returned, the list fields you unioned, and confirmation that the
duplicate is tombstoned, cross-linked and redirected (`redirected_to` quoted from the response).
Never assert the appended body is identical to the duplicate's unless you are quoting a comparison
you actually ran.

## Bulk writes

When the brief hands you many independent item writes (an import, or one revision applied across a
dozen items), send them as ONE `batch_write { operations: [{ tool, args }] }` (1-25 operations,
under 2 MB) instead of one call each; load it with ToolSearch like the rest. `tool` is
`publish_memory`, `patch_memory`, `publish_doc`, `patch_doc`, `publish_entry` or `append_entry`,
and `args` is exactly what that tool takes alone. Every scribe rule applies to each operation
unchanged: the body is authored or patched, never retyped; `kb` (and `repo`) go on the operations
that need them, because each resolves its own KB; every revising operation carries `base_rev` from
a read.

- **It is not atomic.** Operations run in order and a failure does not stop or undo the rest. Read
  every entry of `results`: an `ok: false` carries the standalone call's own error (a
  `rev_conflict` with its diff, a `validation_failed` with its issues); handle it exactly as you
  would that single call.
- **Never resend the whole batch.** The operations that succeeded already landed, and re-running an
  `append_entry` appends it twice. Retry only the failed indices, in a new batch or singly.
- **Single calls stay single:** a retraction, a topic write and a dedupe run are not batchable
  (`unsupported_tool`).
- Report `succeeded`/`failed`, each result's `landed_in`, and every failed index with its error
  verbatim; the envelope's one `vocab_rev` names the first successful operation's KB.

## Topics and categories

Read the vocabulary first: `read_resource({uri: "gitian-kb://vocab", view: "index"})` lists every
live topic with a one-line `summary` and its `aliases`, plus the `tombstoned` slugs to stay away
from. Call `topic` for one topic's full description only when two candidates are close.

Link any existing topic that genuinely fits: 1-3 as `topics` (what the item is *about*), any number
as `mentions` (what it *touches*). **Mint only slugs the brief names**: a novel slug auto-mints as
a permanent, undescribed stub, which is how synonym fragmentation creeps in. When nothing in the
vocabulary fits and the brief named nothing, publish **without** topics and flag it in your report
so the primary can decide; never invent a slug to fill a count, and never link a topic that just
repeats the project or repo name (`project_name_topic`). `category`: at most one, only a slug the
vocab lists, otherwise `null`.

**Describing stubs.** When a write mints stubs (`organic_topics_minted` /
`undescribed_topics_minted`) and the brief supplies their meaning, describe them ALL in ONE
`publish_topic` call (`topics: [{slug, description}, ...]`, 1-50, instead of `slug` +
`description`), never one call per slug, and describe only slugs the response named as minted (an
`unminted_mentions` slug was deliberately not minted: leave it).

**Categories.** `publish_category` (`slug`, `name`, routing `prompt`; create-or-update) is yours
only when the brief names the category and supplies what it is for; on an org KB only the org's
admin may author one, and a `forbidden` there is reported, not worked around. There is no category
retract: removal is a web action.

**`suggested_topics`** is a response *field*, not a warning, so "report warnings verbatim" does not
cover it: up to 5 existing topics the server read as close to what you just wrote but that you
didn't link. Adopt one only when it names what the item is *about* (existing vocabulary only, never
a mint) in a follow-up `patch_doc`/`patch_memory` on `topics`/`mentions` carrying the `base_rev`
that publish just returned; ignore the rest rather than padding a count. Name the ones you adopted
in your report.

## Terminal flips and the journal

**Terminal states** only on an explicit instruction. `landed` means `status: landed`,
`impl_status: done`, the `landed` date, `branch_status: merged`, `commits` populated,
`next_steps`/`blockers` reduced to survivors, and a recap: a `## Implementation recap` section
appended to the doc (`body_append` on the same patch), or a separate `type: recap` doc past ~800
words, cross-linked both ways. A recap without the status flip leaves the KB stale; do both. The
14-point recap checklist is in `spec-authoring.md`. Read the doc with `include_body: false` first:
the flip needs its manifest and `rev`, not its body.

**Commits** are `<7-char-sha>  <subject>` (two spaces), chronological oldest first, append-only.

**The journal is a running record.** `append_entry` is the primary journaling verb: `date`/`scope`
optional (today UTC / `work`), it creates the day's entry or appends a `section` to it,
union-merging `tags`/`topics`/`mentions`/`commits`/`related`, atomic against concurrent writers.
`publish_entry` is only for a genuine full rewrite of a day's entry. The bar for "meaningful" is
what a teammate would care to hear at standup: a pivot, a diagnosis, a settled design, not a rename
or a re-run.

## Routing edge cases

The three routing rules in the scribe's prompt decide every landing. The detail behind them:

- A `publish_doc`/`publish_entry`/`append_entry` with `kb` omitted and a `repo` of `<owner>/<name>`
  whose owner is a gitian org you route to lands in `<org>/team` (the response says so in
  `routed_to`). A `publish_memory` stays in the connection's default KB (`home` unless configured)
  no matter what its `repo` says.
- A create with neither `kb` nor `repo` carries `repo_missing_cannot_route` when you belong to an
  org it could have routed to. That is how a team's journal gets forked silently, so a brief that
  gave you neither is handed back, not guessed.
- Nothing binds a KB for the rest of a session: every call carries its own `kb` or routes on its
  own `repo`.
- `default_kb_unavailable` means the connection's configured default could not be written and the
  write fell back to `home`: report it verbatim like any landing that is not the brief's.
- To move a journal entry to another KB, `append_entry` into the right `kb`; moving a doc is the
  primary's call. A large body moves in several calls, never one giant call: an oversized single
  tool call is what produced the original unparseable-tool-call failure.

Read `kb-targeting.md` when a response carries an org or linked-KB error you don't recognize, or an
`ambiguous_slug` you have to resolve.

## Reports and follow-ups

Keep the report short: you exist to save the primary's context. Beyond the one-line
`<slug> rev <N> → <url>` and every write's `landed_in`, include each only when it applies: every
`warnings` entry verbatim and `routed_to`; your judgment calls (primitive chosen, topics linked or
deliberately omitted, a field you derived rather than copied, `files` derived from the transcript);
for a spec-class doc (`spec`/`plan`/`design`/`handoff`/`recap`) the published `summary` and the
body's heading outline, 150 words at most. Never editorialize on top of a tool response.

**Follow-ups.** A correction, a sign-off answer or a further status flip on the same item may
arrive as a message after you have reported. Your context, including the draft you just wrote, is
still intact: apply the correction with `body_edits`/`patch_*` and report again. You are the only
scribe in flight for the session, so nothing you publish races another scribe.

**When to interrupt.** Interrupting the primary is cheap (your report is delivered at its next turn
boundary, and it never preempts a running tool call) but not free: reserve it for the five sign-off
cases and for a rule that forces a hand-back (`rev_conflict` twice, a body you cannot express as
edits, a dedupe body over the ceiling, a harness refusal such as plan mode).

**Plan-mode reminder.** Under an unhonoured plan mode, draft the manifest and body into the plan
file and return `NEEDS SIGN-OFF` naming plan mode as the blocker; never publish against it.

## Librarian: orientation sweep

The standard read-at-work-start: read the vocabulary (`view: "index"`), `search`/`list` for the
topic at hand, then `neighbors` on the best hit, plus `file_intents` when the work is repo-bound.
The point is that the primary does not spend 4-6 tool calls and their full outputs on orientation.

The brief: what exists (slug + `rev` + a one-line conclusion each, in your own words, never a
pasted summary field); the vocabulary's `vocab_rev`; the live topics (slug + summary for every
topic, not just the rev number: the primary links topics off this list); **"get these:"** the 1-3
slugs to read in full and which sections; any `file_intents`/`contention` hits, naming the
contending slug and the overlapping paths. List every KB label the sweep covered for the repo
(every `kb` your hits carried plus any `own_only_kbs` entry, e.g. "`home`, `acme/team`
(own-only)"): that list is how the primary learns a team KB exists and what to put in a brief's
`kb`. A thin result from an unsubscribed org KB is not "the team has nothing in flight".

## Librarian: targeted digest, change feed and counts

**Targeted digest.** "What did we decide about X?": `search`, then `get` the two or three best
hits (`include_body: false` when the manifest answers it), and answer in your own words with the
slug and `rev` behind each claim. `search` returns best match first: ask in plain words (an item
matching any of them comes back, ones matching all of them rank first). A response whose `mode` is
`"lexical"` had no semantic help, so before reporting that the KB holds nothing, retry once in the
vocabulary a document would use, or `neighbors` the closest hit.

**What changed since.** ONE `changes` call with `since` (the last `created_at` the primary saw, or
the session's start), paged with `next_cursor` until it is `null`. Each row is one revision: `kb`,
`slug`, `rev`, `kind` (a `tombstone` is a retraction), `author_login`, `created_at`, `title`,
oldest first. Never walk `history` item by item to answer this.

**Enumerate or count.** `list` pages with `next_cursor` (pass it back as `cursor`, same filters,
until it is `null`), and every page carries `total` for the whole filtered set. For a count, one
`list` with `facets: true` answers it (`total` plus counts by primitive, type, status, category,
top topics and tags) without paging. Never approximate a count from per-topic `topic` calls or a
capped page.

## Librarian: vocab-delta refresh

A tool response's `vocab_rev` differs from the value the primary last saw. Re-read the vocabulary
and diff it against what the primary saw last; report only what changed, never the whole
vocabulary again. The index view shows new slugs, newly described stubs (a `summary` that turned
non-null), `aliases` gained or lost (a merge or unmerge also bumps `vocab_rev`) and the `tombstoned`
list. Read the full view (no `view`) when the diff must also cover freshness or a topic crossing
into or out of `dormant`.

Shape: "since vocab_rev 41: +2 new topics (`x`, `y`, both still undescribed stubs), 1 newly
described (`z`), 1 tombstone (`w`), `a` merged into `b` (alias), `c` went dormant." If nothing
changed since the last seen rev, say so in one line.

## Librarian: dedupe candidates

`neighbors` the doc at high weight and keep the hits sharing its `repo`, plus a `search` on its
title. Report each pair with both slugs, their `rev`s, their shared primary topics and overlapping
`files`, and one line on why they look duplicated. **Proposing is where you stop**: the primary
decides which one survives and dispatches `kb-scribe` to merge. No recommendation of which should
survive.

## Librarian: why the budget and the vocabulary read exist

One orientation sweep once cost 202k tokens and 25 tool calls, most of it reconstructing the
vocabulary with per-topic `topic` calls because the agent did not know `read_resource` reaches
`gitian-kb://vocab`. One `read_resource` returns the whole live vocabulary, categories included,
and the 8-call budget keeps a pull cheaper than the primary doing it inline. When the budget runs
out before the question is answered, a partial brief with its edges named is useful; a 25-call
exploration is not.
