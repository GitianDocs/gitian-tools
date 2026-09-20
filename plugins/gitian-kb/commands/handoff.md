---
description: Distill the current conversation state into a KB handoff doc — run before /compact or before stepping away
allowed-tools: Bash(git remote get-url:*), Bash(git branch:*), Bash(date:*)
---

Preloaded context:

- Repo remote: !`git remote get-url origin 2>/dev/null || echo "(none)"` — normalize it to
  `owner/name` before briefing it
- Branch: !`git branch --show-current 2>/dev/null || echo "(none)"`
- Today (UTC): !`date -u +%Y-%m-%d`

Capture the current conversation's working state in the gitian KB as a handoff, per the gitian-kb
skill — **brief `kb-scribe`, don't type the doc yourself**. Include the session transcript path and
the transcript-extract command from the session context: the substance is in the conversation, and
the scribe distills it rather than making you re-emit it.

1. If a governing doc for this thread already exists, `get` it first (`include_body: false` is
   enough) and brief a **revision** of that slug with the handoff state folded in — `patch_doc`,
   `base_rev` from the read you just made, `body_edits` for text that moves and `body_append` for
   a new section — instead of minting a duplicate. Otherwise brief a new `publish_doc` with
   `type: handoff`.
2. The brief carries what only you know: the task and where it stands, decisions in flight and
   why, what is **verified** versus **assumed**, next steps, blockers, the `repo` (`owner/name`),
   branch and commits above, and the **`kb`** — the KB named in this thread, the revised doc's own
   `kb` label, or `auto`. Ask for it to be written so a fresh agent with no other context could
   resume from the doc alone.
3. Read back the report — slug, rev, url, the `landed_in` KB, `warnings` verbatim — surface the url,
   then tell the user it's safe to `/compact` or step away.

If you cannot dispatch a subagent, read the skill's `references/authoring.md` and publish inline.
