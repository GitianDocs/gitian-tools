---
description: Run a gitian instrumentation pass over the current changes
allowed-tools: Bash(git:*), Bash(cat:*), Bash(head:*)
---

Changed files (working tree vs `HEAD`): !`git -C . diff --name-only HEAD`

Project config, first 40 lines if present (`.gitian/config.yaml` — categories, kinds, defaults):
!`cat .gitian/config.yaml 2>/dev/null | head -40`

Annotate the changed files above per the gitian-docs skill and the canonical spec
(`https://gitian.dev/gitian-adoption-prompt.md`). For each file, apply the duty table: update any
`@gitian` annotation whose described behavior changed, add an annotation for a newly-introduced
construct worth flagging (security-sensitive, a footgun, a real TODO, a new API endpoint), remove
annotations describing deleted code, and sync any paired `docs/<file>.md` (or `.docs/<file>.md`)
the change invalidated. If a project config was found above, respect its categories/kinds/defaults
instead of the built-in ones. Skip files with no paired doc and no annotation-worthy change —
don't manufacture coverage, and never alter runtime behavior while doing this pass.
