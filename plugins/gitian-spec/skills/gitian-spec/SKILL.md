---
name: gitian-spec
description: Use when asked to write, create, or update any spec, plan, design, brainstorm, handoff, or session note, and when documenting what a landed feature shipped — the deliverable is a gitian KB doc (publish_doc/publish_entry via the gitian-kb connection), never a loose markdown file in a repo or vault.
allowed-tools: Bash(git remote get-url:*), Bash(git branch:*), Bash(git rev-parse:*), Bash(git worktree list:*), Bash(git log:*), Bash(date:*), Bash(basename:*), Bash(head:*)
---

# gitian-spec

Long-form work documents — specs, plans, designs, brainstorms, handoffs, session notes, recaps —
are KB deliverables, not files. When someone asks for a design doc, the deliverable is a published
KB doc and its URL, never a loose markdown file in the repo or a vault.

**You don't type the doc: you brief the scribe.** The design is already in this conversation, so
re-emitting it on the orchestrating model is pure duplication. Your job is to scan the KB, hand
`kb-scribe` a brief plus the session transcript, and check what came back.

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
  memories, and the two subagents: `kb-librarian` (reads) and `kb-scribe` (every write).

gitian-kb is a **required companion**: gitian-spec deliberately ships no MCP config of its own,
so the `gitian` tools this skill authors through (`search`, `neighbors`, `get`, `history`,
`file_intents`, `publish_doc`, `patch_doc`, `append_entry`, `publish_entry`) come from
gitian-kb's single connection — if the tools are missing, install `gitian-kb@gitian-tools`.
gitian-kb's publishing rules apply here unchanged and by reference: full-manifest publishes
(explicit `null`/`[]`, keys never omitted), revising an existing doc passes
`base_rev` from the `get` you just made (omit it and the write is refused with
`base_rev_required`; a stale one comes back as `rev_conflict`, to be re-read and re-applied,
never resubmitted with the number swapped), mid-body revisions go through `body_edits` rather
than a re-published body, schema authority (live `gitian-kb://format/*` resources beat cached tool
schemas), `warnings` are advice to act on, and never auto-publish. An explicit authoring request
("write me a design doc for X") *is* explicit publish intent — scan, brief, surface the returned
url.

## The flow

1. **Scan** (below) — yours, or `kb-librarian`'s if it is several calls deep.
2. **Surface findings** to the user in one short paragraph *before* the doc is written: "I found N
   related artifacts: … most relevant are …".
3. **Brief `kb-scribe`** in the background — the brief template lives in the gitian-kb skill. For a
   spec-class doc, always include the session **transcript path and the transcript-extract command**
   from the session context: that is what lets the scribe author the design instead of you retyping
   it. Name the `type`, the slug (when revising), the decisions *with the why*, the rejected
   alternatives, the manifest facts below, and — explicitly — any terminal flip.
4. **Read back** the scribe's report: `slug rev N → url`, the published `summary`, the heading
   outline, `warnings` verbatim. That is the read-back for a spec-class doc — you do not re-read
   the body. Surface the url. A correction goes back to the same scribe via `SendMessage`; it
   applies it with `body_edits`.

One scribe at a time. A `NEEDS SIGN-OFF` report means nothing was published and the scribe needs
one answer — give it, don't re-brief.

## Scan before you write

Never draft into a vacuum. Before briefing:

1. `search` the KB for the topic (plus `list` when the corpus is small), then `neighbors` on the
   best hit — its topic-derived neighborhood surfaces adjacent decisions keyword search misses.
2. Read the plausibly-related artifacts with `get` — frontmatter plus opening section, not
   filename-guessing. Cap the scan at ~5 artifacts; when more match, pick the 2-3 most relevant.
3. **Adopt, don't re-litigate.** A decision resolved in a prior spec is a precondition, not an
   open question — unless the user explicitly reopens it. Say so in the brief.
4. Every genuinely related artifact goes in the brief as a cross-link: its slug for `related`, and
   where the connection is load-bearing, a `[[slug]]` wikilink in the body.
5. Flag stale manifests you trip over (status says in-progress, the work clearly landed) and offer
   to fix them — a fix is its own brief.
6. Before a plan, check `file_intents` for the repo — on overlap, `get` the contending doc and pass
   it to the scribe for `related`.

## Choosing the shape

| You're writing… | Publish as |
|---|---|
| Spec / plan / design for feature-shaped work | `publish_doc`, matching `type`, stable slug, `feature` set |
| Brainstorm | `publish_doc`, `type: design`, `status: draft` |
| Handoff | `publish_doc`, `type: handoff` |
| Session notes / what happened today | `append_entry` (`scope: work`) — or a `type: handoff` doc if it must carry a manifest; `publish_entry` only for a full rewrite of an existing entry |
| Progress update on existing work | revise the **same** doc slug (`patch_doc`, carrying `base_rev`) — revisions are the progress trail; never mint `-v2` slugs |
| What a landed feature shipped | implementation recap (below) + the terminal status flip |

Promoting a session into a feature: mint a doc slug for the feature and cross-link today's entry
— the entry stays where it is. Project-wide reference docs (architecture overviews, runbooks)
have no good `type` yet — say so, pick the least-bad fit (`design`), and don't invent enum
values.

## Manifest facts the brief must carry

The authority for fields and enums is `gitian-kb://format/doc` (or `gitian-kb://format/entry`) and
the scribe reads it. What it cannot derive without you:

- `kb` — the target, never blank: the KB the human named, else the `kb` label of the hit being
  revised, else `auto` (let `repo` routing decide).
- `project` from the working directory — never the doc title. Sibling worktrees (`repo-feature`)
  share the parent repo's project; closely named repos (`minga` vs `minga-platform`) are distinct
  projects: exact basename, never a prefix match.
- `repo` from `git remote get-url origin` as `owner/name`; explicit `null` if none.
- `status` is the DOCUMENT's lifecycle, `impl_status` the CODE's — they legitimately diverge.
- `branch`/`worktree` from the context above, not from memory; `null` plus `n/a` outside a repo.
- `next_steps` imperative ("Land the auth PR"), never questions; `blockers` = what blocks motion.
- `files` — the repo-relative paths the work will touch (trailing `/` claims a subtree), from the
  plan's own Files sections or `git diff --name-only`, never memory.
- Dates absolute (YYYY-MM-DD) from the clock; relative dates converted before they reach the brief.
- Topic slugs only when a genuinely new concept needs minting — otherwise the scribe links existing
  vocabulary and flags a gap.

## Commits list and the implementation recap

`commits` items are `<7-char-sha>  <subject>` — two spaces, both parts, chronological oldest
first, append-only, derived from `git log --oneline` above rather than memory. A squash-merge lists
the single squash commit.

A recap is required when a feature reaches `impl_status: done` and refreshed when it lands.
Default placement: a `## Implementation recap` section appended to the canonical doc's body; only
spin off a separate `type: recap` doc past ~800 words, cross-linked both ways via `related`.
Sessions get a short Findings/Outcome section in the entry, never a separate recap. The full
14-point checklist (schema, API surface, eventing, deferred items, …) lives with the scribe, in the
gitian-kb skill's `references/spec-authoring.md` — ask for depth scaled to surface area: one
paragraph for a bugfix, the whole checklist for multi-service, schema, or API work.
`/gitian-spec:recap` runs this close-out end to end.

## When you cannot brief a scribe

A subagent cannot spawn a subagent, and dispatching may be unavailable. Then author inline: load
the `gitian-kb` skill and read its `references/authoring.md` (the publishing discipline) and
`references/spec-authoring.md` (manifest derivation and the recap checklist), and publish yourself.

## Output rules

Manifest completeness over prose. Bodies are distilled intent documentation — Obsidian-flavored,
`[[slug]]` wikilinks, per gitian-kb's writing-bodies rules (including never injecting `@gitian`
markup into a codebase that hasn't opted into the gitian docs system) — not transcripts. Hold the
scribe to that when you read its report: a body that reads as a conversation log is a re-brief, not
a fix to make yourself.
