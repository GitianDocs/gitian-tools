# Spec-class authoring: specs, plans, designs, handoffs, recaps

The long-form half of KB authoring. A request to write a spec, plan, design, brainstorm, handoff or
session note is a KB deliverable — **never a loose markdown file in a repo or a vault** — and this
file is how one is built. It pairs with `authoring.md` (the publishing rules, `base_rev`, patching)
and `topics.md` (linking); nothing here repeats them.

The companion **gitian-spec** plugin owns the *routing* for these requests (scan the KB, brief the
scribe, read the result back) and keeps the short conventions its own users need. The derivation
rules and the recap checklist live here, with the agent that writes them.

## Choosing the shape

| The ask… | Publish as |
|---|---|
| Spec / plan / design for feature-shaped work | `publish_doc`, matching `type`, stable slug, `feature` set |
| Brainstorm | `publish_doc`, `type: design`, `status: draft` |
| Handoff | `publish_doc`, `type: handoff` |
| Session notes / what happened today | `append_entry` (`scope: work`) — or a `type: handoff` doc if it must carry a manifest; `publish_entry` only for a full rewrite of an existing entry |
| Progress update on existing work | revise the **same** doc slug (`patch_doc`, carrying `base_rev`) — revisions are the progress trail; never mint `-v2` slugs |
| What a landed feature shipped | implementation recap (below) + the terminal status flip |

Promoting a session into a feature: mint a doc slug for the feature and cross-link today's entry —
the entry stays where it is. Project-wide reference docs (architecture overviews, runbooks) have no
good `type` yet — say so, pick the least-bad fit (`design`), and don't invent enum values.

## Scanning before writing

Never draft into a vacuum. The primary (or `kb-librarian`) normally does this before the brief
exists; when it hasn't, do it first:

1. `search` the KB for the topic (plus `list` when the corpus is small), then `neighbors` on the
   best hit — its topic-derived neighborhood surfaces adjacent decisions keyword search misses.
2. Read the plausibly-related artifacts with `get` — frontmatter plus opening section, not
   filename-guessing. Cap the scan at ~5 artifacts; when more match, pick the 2-3 most relevant.
3. **Adopt, don't re-litigate.** A decision resolved in a prior spec is a precondition, not an open
   question — unless the brief explicitly reopens it.
4. Cross-link every genuinely related artifact: its slug in `related`, a `[[slug]]` wikilink in the
   body where the connection is load-bearing.
5. Flag stale manifests you trip over (status says in-progress, the work clearly landed) in the
   report — offer the fix, don't apply it unasked.

## Deriving the manifest

The authority for fields and enums is `gitian-kb://format/doc` — do not add fields beyond it. The
rules the schema can't express:

- `project` derives from the working directory — never from the doc title or a pre-existing value.
  Sibling worktrees (`repo-feature`) share the parent repo's project. Closely named repos
  (`minga` vs `minga-platform`) are distinct projects: exact basename, never a prefix match.
- `repo` from `git remote get-url origin`, normalized to `owner/name`; explicit `null` if none.
- `status` is the DOCUMENT's lifecycle; `impl_status` is the CODE's. They legitimately diverge — a
  spec can be `landed` while `impl_status` is still `in-progress`.
- `branch`/`worktree` are the code's location at write time — from `git rev-parse --abbrev-ref HEAD`
  / `git worktree list`; `null` plus `n/a` statuses when not in a repo. Never trust remembered
  values; when the brief states them, the brief is the ground truth for the session it came from.
- `next_steps` in imperative voice ("Land the auth PR"), never questions — it is the agent-facing
  TODO list. `blockers` = anything preventing forward motion; `[]` if none.
- Dates from the system clock, never memory; convert relative dates ("Tuesday") to absolute
  YYYY-MM-DD.
- `files` — the repo-relative paths the work will touch, from the plan's own Files sections or
  `git diff --name-only`, never memory; a trailing `/` claims a subtree; `[]` only when the doc
  isn't code-shaped. Before publishing a plan, check `file_intents` for the repo — on overlap,
  `get` the contending doc and cross-link it in `related`.
- `topics`/`mentions` — derive from the work's actual subject matter (what the doc is *about* vs.
  what it merely *touches*), never invented and never padded to hit a count. See `topics.md`.
- `category` — at most one, chosen from `gitian-kb://vocab`'s categories via their routing prompts,
  never guessed from the doc type; `null` when nothing in the vocabulary fits.
- `summary` — always present. On a spec-class doc it is also what gets read back to the primary, so
  make it carry the outcome, not the subject line.

## Updating an existing doc

`get` the full doc first; skim `history` if you didn't write it. Re-derive every manifest field
from ground truth — pre-existing values go stale (dead branches, statuses nobody flipped). Only
`started` is immutable history. Revise with `patch_doc` (+ `body_edits` for text inside the body);
a full re-publish of an existing body is the one thing never to do.

## Commits list

Populate when work lands (or incrementally as commits accrue). Each item is
`<7-char-sha>  <subject>` — two spaces between, both parts required. Chronological, oldest first.
Append-only: never reorder. A squash-merge lists the single squash commit; a multi-branch feature
lists all commits chronologically regardless of branch. Derived from `git log --oneline` or from
the brief, never from memory.

## Implementation recap

Required when a feature reaches `impl_status: done` (even if unmerged); refreshed when it lands.
Default placement: append a `## Implementation recap` section to the canonical doc's body
(`body_append` on the same `patch_doc` that flips the manifest). Only spin off a separate
`type: recap` doc when the recap exceeds ~800 words — then cross-link both ways via `related`.
Sessions get a short Findings/Outcome section in the entry, never a separate recap.

Scale depth to surface area — a bugfix gets one paragraph (root cause + fix); multi-service,
schema, or API work gets the full checklist:

1. **Mental model** — one paragraph: what shape it took, where it lives.
2. **Schema changes** — every migration chronologically, exact table/column/FK names; state
   "none" explicitly.
3. **Proto/IDL changes** — every new or extended message (field names + numbers), every new RPC.
4. **Domain layer** — new/extended types, constants, validation.
5. **Data layer** — new repo methods with signatures, codegen notes.
6. **API surface** — EVERY endpoint/RPC: name + signature, one-line semantics, auth/role
   requirements, side effects. Never abbreviate this section — it is the highest-leverage
   information for future readers.
7. **Eventing** — subjects emitted/consumed, wiring location, atomicity gaps.
8. **Integration touchpoints** — which services/packages, via what (RPC, events, shared DB),
   inversion-of-control gotchas.
9. **Test coverage** — a line per test package: status + what it covers.
10. **Commits** — full chronological list with one-line subjects.
11. **Deferred items** — distinguish "designed away" from "tracked for v2".
12. **Known issues / housekeeping** — pre-existing bugs found, config weirdness, anything a
    future agent will trip over.
13. **Hard prerequisites for activation** — shipped-but-dormant conditions.
14. **What's next** — concrete, imperative.

Style: tables for migration/RPC/event lists; exact identifiers (future agents grep for them);
describe what shipped, not why (the design above covers why); no editorializing — an honest
"known atomicity gap" beats "this is solid".

## Terminal-state discipline (field level)

Non-negotiable, shared with `authoring.md`. When work reaches a terminal state the manifest moves
with it — but only when the brief says so explicitly:

- **Landed** — `status: landed`, `impl_status: done`, `landed` date set, `branch_status: merged`,
  `worktree_status` reflecting reality (removed or active), `next_steps: []` (or only surviving
  follow-ups), `blockers: []`, `commits` populated. A landed doc with empty commits is a manifest
  bug (the server warns: `landed_without_commits`).
- **Abandoned** — `status: abandoned`, `impl_status: reverted` or `n/a`, the one-line why in
  `summary`.
- **Paused / blocked** — the reason in `blockers`; never leave a stalled doc looking active.

## Output rules

Manifest completeness over prose. Bodies are distilled intent documentation — Obsidian-flavored,
`[[slug]]` wikilinks, per `authoring.md`'s writing-bodies rules (including never injecting
`@gitian` markup into a codebase that hasn't opted into the gitian docs system) — not transcripts.
A spec-class publish reports back the published `summary` plus the heading outline, so the primary
can check the shape without re-reading the body.
