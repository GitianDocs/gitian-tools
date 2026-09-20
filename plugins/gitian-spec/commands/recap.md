---
description: Close out a landed feature — flip its KB doc to a terminal state and write the implementation recap
allowed-tools: Bash(git remote get-url:*), Bash(git branch:*), Bash(git log:*), Bash(date:*)
---

Preloaded context:

- Repo remote: !`git remote get-url origin 2>/dev/null || echo "(none)"`
- Branch: !`git branch --show-current 2>/dev/null || echo "(none)"`
- Today (UTC): !`date -u +%Y-%m-%d`
- Recent commits: !`git log --oneline -15 2>/dev/null || echo "(none)"`

A feature just landed (or reached `impl_status: done`). Close it out per the gitian-spec skill:

1. Find the governing doc — `search`/`list` the KB, `get` it (frontmatter plus enough body to see
   what the recap must not repeat; skim `history` if you didn't write it). If none exists, say so
   and brief a new doc instead of recapping into the void. Note its `rev`.
2. Brief `kb-scribe` in the background with the close-out, stated explicitly because a terminal
   flip is never inferred: `patch_doc` with `base_rev` from step 1, `status: landed`,
   `impl_status: done`, the `landed` date from the clock above, `branch_status: merged`, `commits`
   from `git log --oneline` above (each item `<7-char-sha>  <subject>`, two spaces, oldest first;
   a squash-merge lists the single squash commit), `next_steps` reduced to survivors,
   `blockers: []`. The patch carries no body, so the doc cannot be truncated on the way; if the rev
   moved since step 1 the scribe re-reads and re-applies rather than resending with the new number.
3. In that same brief, ask for the implementation recap scaled to the change's surface area — what
   shipped, the key decisions, gotchas, deferred items — appended as `## Implementation recap` via
   `body_append` on the same `patch_doc`, or a separate `type: recap` doc cross-linked both ways
   via `related` when it exceeds ~800 words. The scribe holds the full checklist
   (`references/spec-authoring.md`); give it the facts, not the prose. Pass the session transcript
   path and the transcript-extract command so it can distill what actually happened.
4. Read back the scribe's report — slug, rev, url, `warnings` verbatim — and surface every url to
   the user. A correction goes back to the same scribe via `SendMessage`.
