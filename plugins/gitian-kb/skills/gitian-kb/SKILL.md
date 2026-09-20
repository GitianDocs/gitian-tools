---
name: gitian-kb
description: Use when starting work (dispatch `kb-librarian` to orient in the gitian Knowledge Base) and at completion points — a settled design, a written plan, a durable fact worth remembering, a handoff, a day worth recording, or a landed feature — to brief `kb-scribe` with what mattered so it publishes the distilled item.
allowed-tools: Bash(git remote get-url:*), Bash(git branch:*), Bash(date:*)
---

# gitian-kb

The gitian Knowledge Base (KB) is durable memory git history doesn't give you: rationale,
decisions, status, next steps. **You decide WHEN a publish is warranted and WHAT mattered; two
subagents do the rest** — `kb-librarian` reads, `kb-scribe` writes (every publish, revision,
append, retraction, dedupe merge).

- Repo remote: !`git remote get-url origin 2>/dev/null || echo "(none)"` — normalize it to
  `owner/name` (drop scheme/host and `.git`) before you brief it
- Branch: !`git branch --show-current 2>/dev/null || echo "(none)"`
- Today (UTC): !`date -u +%Y-%m-%d`

## When to publish — trigger table

| Trigger | Brief the scribe for |
|---|---|
| Design settled | a `doc`, `type: spec` or `design` |
| Plan written | a `doc`, `type: plan` |
| Durable fact learned | a `memory` — a preference, gotcha or fact |
| Meaningful event, pivots included | a journal append (`append_entry`) |
| Handing off, or a compaction squashed context | a `doc`, `type: handoff` (*manual* `/compact`: `/gitian-kb:handoff` first) |
| Conversation pivots off a thread | an update to the old thread's item *before* the new topic, + a journal append |
| Feature landed | the terminal `status` flip **and** a recap — both, or the KB goes stale |
| Status/commits/next_steps moved | a revision of that slug, never a new one |

**Never auto-publish** — completion points and explicit user intent only; never silently, in bulk,
or on a timer. A pivot or a wrapped session clears the bar; a rename or a re-run does not.

## Dispatching

- **Orient before substantive work, not just before writing.** Dispatch the librarian at work
  start: vocab, `search`, `neighbors` on the best hit, `file_intents` for the repo
  (`include_landed: true` widens it to who reworked those paths) — on overlap with paths you are
  about to touch, read the contending doc first.
- **Background, one at a time.** Types are `gitian-kb:kb-librarian` and `gitian-kb:kb-scribe`. At
  most one scribe and one librarian in flight; a follow-up on the same item goes to the **live
  scribe via `SendMessage`**, unrelated work waits for the report.
- **Reads you will act on, you make yourself.** `get` the doc you are about to follow; a one-off
  `search`/`get` stays inline — spawning an agent costs more. On `ambiguous_slug`, retry with a
  `kb` from its `candidates`, never a guess. Dispatch the librarian for a multi-call sweep, a
  digest, or a vocab-delta refresh when `vocab_rev` moves.
- **Writes are never inline.** Brief the scribe.

## The brief (10-20 lines)

Required every time — the scribe has no cwd and no conversation:

- **intent** — publish | revise | journal | retract | dedupe
- **primitive / type** — memory | doc (`spec`/`plan`/`design`/`handoff`/`recap`) | entry
- **kb** — the target, never blank: the KB the human named, else the `kb` label of the sweep hit
  being revised, else `auto` (let `repo` routing decide). **A KB the human names is the brief's
  `kb` for every write in that thread.**
- **repo / branch** — `owner/name` (normalized above) + the branch. `auto` needs `repo`: with
  neither, a write can only land in `home`. Docs and journal entries with an org-owned `repo` route
  to `<org>/team`; memories never route. Every write answers `landed_in` — the scribe reports it
  and hands back `NEEDS SIGN-OFF` on a landing that isn't the brief's `kb`.
- **project / files** — the `project`, and the paths a plan or code-shaped doc will touch
  (`files`), else no file intents at all
- **what happened, and the decisions *with the why*** — only you have it. Tool results never
  reach the scribe: a finding from a log or query output goes in the brief

Whatever else applies: **target slug** (revising, retracting, merging — survivor **and** duplicate
for a dedupe); **rejected alternatives**; **status / impl_status** — state a terminal flip or
retraction explicitly, the scribe infers neither; **pr / commits**; **topics to mint** — only a
genuinely new concept; **transcript + transcript-extract** — both session-context lines when the
conversation holds the substance.

## Reports and sign-off

A success report is one line — `slug rev N → url` — plus the `landed_in` KB, `warnings` verbatim,
the judgment calls, and for a spec-class doc its `summary` and heading outline — the body is
unread, so `get` a high-stakes one once. Warnings are advice: relay them, fix later.

**`NEEDS SIGN-OFF`** means nothing was published: create-vs-revise unclear, the brief contradicting
the transcript or the doc, an unstated terminal flip or retraction, an edit that would drop
content, or a landing that isn't the `kb` you briefed. It carries a ≤100-word draft and one
question. Answer with `SendMessage` to that same scribe; it then publishes.

## When you cannot spawn agents

A subagent cannot spawn a subagent. If you are one (or dispatching is unavailable), read
`references/authoring.md` beside this file and work inline. Also there: `kb-targeting.md` (the `kb`
argument, routing, linked and org KBs), `topics.md` (topics, mentions, categories),
`spec-authoring.md` (manifests, the recap checklist). Companions: **gitian-spec** owns
spec/plan/design *requests*, **gitian-docs** annotations.

## The nudge layer

Hooks nudge off harvested MCP traffic — advisory, fail-open, **silent when compliant**, satisfied
by a brief you dispatched; `/gitian-kb:status` inspects them. One is a hard precondition: a doc or
journal-entry write carrying neither `kb` nor `repo` is refused, since it could only land in `home`.
