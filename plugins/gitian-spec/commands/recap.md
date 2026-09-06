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

1. Find the governing doc — `search`/`list` the KB, `get` it in full (skim `history` if you
   didn't write it). If none exists, say so and mint one instead of recapping into the void.
2. Patch it (`patch_doc` with `base_rev` from step 1) to the terminal manifest, re-derived from
   ground truth, never from memory: `status: landed`, `impl_status: done`, `landed` date from the
   system clock, `branch_status: merged`, `commits` from `git log --oneline` above (each item
   `<7-char-sha>  <subject>`, two spaces, oldest first; a squash-merge lists the single squash
   commit), `next_steps` reduced to survivors, `blockers: []`. The patch carries no body, so the
   doc cannot be truncated on the way; if the rev moved since step 1, re-read and re-apply rather
   than resending with the new number.
3. Author the implementation recap per the skill's checklist, scaled to the change's surface
   area — appended as `## Implementation recap` in the canonical doc's body (`body_append` on the
   same `patch_doc`, which only ever grows the body), or a separate `type: recap` doc cross-linked
   both ways via `related` when it exceeds ~800 words.
4. Surface every returned `url` to the user.
