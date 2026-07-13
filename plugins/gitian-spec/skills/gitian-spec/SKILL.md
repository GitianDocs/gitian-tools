---
name: gitian-spec
description: Use when asked to write, create, or update any spec, plan, design, brainstorm, handoff, or session note, and when documenting what a landed feature shipped — the deliverable is a gitian KB doc (publish_doc/publish_entry via the gitian-kb connection), never a loose markdown file in a repo or vault.
allowed-tools: Bash(git remote get-url:*), Bash(git branch:*), Bash(git rev-parse:*), Bash(git worktree list:*), Bash(git log:*), Bash(date:*), Bash(basename:*), Bash(head:*)
---

# gitian-spec

Long-form work documents — specs, plans, designs, brainstorms, handoffs, session notes, recaps —
are KB deliverables, not files. When someone asks for a design doc, the deliverable is a published
KB doc and its URL, never a loose markdown file in the repo or a vault. This skill is the
authoring discipline; the companion gitian-kb plugin owns the connection and the publishing rules.

## Preloaded context

- Repo remote: !`git remote get-url origin 2>/dev/null || echo "(none)"`
- Branch: !`git branch --show-current 2>/dev/null || echo "(none)"`
- Directory (project derives from this): !`basename "$PWD"`
- Worktrees: !`git worktree list 2>/dev/null | head -4 || echo "(none)"`
- Today (UTC): !`date -u +%Y-%m-%d`
- Recent commits: !`git log --oneline -8 2>/dev/null || echo "(none)"`

## Boundaries (companion: gitian-kb)

- **gitian-spec (this skill)** — doc-as-deliverable authoring: someone asks for a spec, plan,
  design, brainstorm, handoff, or session note, or a landed feature needs its shipped state
  documented.
- **gitian-kb** — orientation (RAG at work-start), completion-point distillation, the journal,
  and memories.

gitian-kb is a **required companion**: gitian-spec deliberately ships no MCP config of its own,
so the `gitian` tools this skill authors through (`search`, `neighbors`, `get`, `history`,
`file_intents`, `publish_doc`, `publish_entry`) come from gitian-kb's single connection — if the tools are
missing, install `gitian-kb@gitian-tools`. gitian-kb's publishing rules apply here unchanged and
by reference: full-manifest publishes (explicit `null`/`[]`, keys never omitted), schema
authority (live `gitian-kb://format/*` resources beat cached tool schemas), `warnings` are
advice to act on, and never auto-publish. An explicit authoring request ("write me a design doc
for X") *is* explicit publish intent — author, publish, surface the returned url. Don't restate
the schema from memory: read `gitian-kb://format/doc` (or `gitian-kb://format/entry`) before the
first publish of a session.

## Scan before you write

Never draft into a vacuum. Before writing anything:

1. `search` the KB for the topic (plus `list` when the corpus is small), then `neighbors` on the
   best hit — the likeness graph surfaces adjacent decisions keyword search misses.
2. Read the plausibly-related artifacts with `get` — frontmatter plus opening section, not
   filename-guessing. Cap the scan at ~5 artifacts; when more match, pick the 2-3 most relevant.
3. Surface findings to the user in one short paragraph *before* drafting: "I found N related
   artifacts: … most relevant are …".
4. **Adopt, don't re-litigate.** A decision resolved in a prior spec is a precondition, not an
   open question — unless the user explicitly reopens it.
5. Cross-link every genuinely related artifact: its slug in `related`, a `[[slug]]` wikilink in
   the body where the connection is load-bearing.
6. Flag stale manifests you trip over (status says in-progress, the work clearly landed) and
   offer to fix them.

## Choosing the shape

| You're writing… | Publish as |
|---|---|
| Spec / plan / design for feature-shaped work | `publish_doc`, matching `type`, stable slug, `feature` set |
| Brainstorm | `publish_doc`, `type: design`, `status: draft` |
| Handoff | `publish_doc`, `type: handoff` |
| Session notes / what happened today | `publish_entry` (`scope: work`) — or a `type: handoff` doc if it must carry a manifest |
| Progress update on existing work | re-publish the **same** doc slug — revisions are the progress trail; never mint `-v2` slugs |
| What a landed feature shipped | implementation recap (below) + the terminal status flip |

Promoting a session into a feature: mint a doc slug for the feature and cross-link today's entry
— the entry stays where it is. Project-wide reference docs (architecture overviews, runbooks)
have no good `type` yet — say so, pick the least-bad fit (`design`), and don't invent enum
values.

## Deriving the manifest

The authority for fields and enums is `gitian-kb://format/doc` — do not add fields beyond it.
The rules the schema can't express:

- `project` derives from the working directory — never from the doc title or a pre-existing
  value. Sibling worktrees (`repo-feature`) share the parent repo's project. Closely named repos
  (`minga` vs `minga-platform`) are distinct projects: exact basename, never a prefix match.
- `repo` from `git remote get-url origin`, normalized to `owner/name`; explicit `null` if none.
- `status` is the DOCUMENT's lifecycle; `impl_status` is the CODE's. They legitimately diverge —
  a spec can be `landed` while `impl_status` is still `in-progress`.
- `branch`/`worktree` are the code's location at write time — from
  `git rev-parse --abbrev-ref HEAD` / `git worktree list`; `null` plus `n/a` statuses when not
  in a repo. Never trust remembered values.
- `next_steps` in imperative voice ("Land the auth PR"), never questions — it is the
  agent-facing TODO list. `blockers` = anything preventing forward motion; `[]` if none.
- Dates from the system clock, never memory; convert relative dates ("Tuesday") to absolute
  YYYY-MM-DD.
- `files` — the repo-relative paths the work will touch, from the plan's own Files sections or
  `git diff --name-only`, never memory; a trailing `/` claims a subtree; `[]` only when the doc
  isn't code-shaped. Before publishing a plan, check `file_intents` for the repo — on overlap,
  `get` the contending doc (pass its `owner` login when the hit carries one — a teammate's plan
  on a shared org repo, not your own) and cross-link it in `related`.

## Updating an existing doc

`get` the full doc first; skim `history` if you didn't write it. Re-derive every manifest field
from ground truth — pre-existing values go stale (dead branches, statuses nobody flipped). Only
`started` is immutable history. Re-publishing the same slug appends a revision.

## Terminal-state discipline

Non-negotiable, shared with gitian-kb: when work reaches a terminal state, update the manifest
BEFORE considering the task complete. Field-level expectations:

- **Landed** — `status: landed`, `impl_status: done`, `landed` date set,
  `branch_status: merged`, `worktree_status` reflecting reality (removed or active),
  `next_steps: []` (or only surviving follow-ups), `blockers: []`, `commits` populated. A landed
  doc with empty commits is a manifest bug (the server warns: `landed_without_commits`).
- **Abandoned** — `status: abandoned`, `impl_status: reverted` or `n/a`, the one-line why in
  `summary`.
- **Paused / blocked** — the reason in `blockers`; never leave a stalled doc looking active.

## Commits list

Populate when work lands (or incrementally as commits accrue). Each item is
`<7-char-sha>  <subject>` — two spaces between, both parts required. Chronological, oldest
first. Append-only: never reorder. A squash-merge lists the single squash commit; a multi-branch
feature lists all commits chronologically regardless of branch. Derived from
`git log --oneline`, never from memory.

## Implementation recap

Required when a feature reaches `impl_status: done` (even if unmerged); refreshed when it lands.
Default placement: append a `## Implementation recap` section to the canonical doc's body. Only
spin off a separate `type: recap` doc when the recap exceeds ~800 words — then cross-link both
ways via `related`. Sessions get a short Findings/Outcome section in the entry, never a separate
recap.

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

## Output rules

Manifest completeness over prose. Bodies are distilled intent documentation — Obsidian-flavored,
`[[slug]]` wikilinks, per gitian-kb's writing-bodies rules (including never injecting `@gitian`
markup into a codebase that hasn't opted into the gitian docs system) — not transcripts.
