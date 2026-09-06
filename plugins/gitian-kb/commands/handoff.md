---
description: Distill the current conversation state into a KB handoff doc — run before /compact or before stepping away
allowed-tools: Bash(git remote get-url:*), Bash(git branch:*), Bash(date:*)
---

Preloaded context:

- Repo remote: !`git remote get-url origin 2>/dev/null || echo "(none)"`
- Branch: !`git branch --show-current 2>/dev/null || echo "(none)"`
- Today (UTC): !`date -u +%Y-%m-%d`

Publish the current conversation's working state to the gitian KB as a handoff, per the gitian-kb
skill. Read `gitian-kb://format/doc` first if you haven't this session. Then `publish_doc` with
`type: handoff` — full manifest, never omit keys — capturing: the task and where it stands,
decisions in flight (and why), what's verified versus assumed, next steps, and blockers. Write it
so a fresh agent with no other context could resume the work from the doc alone.

If a governing doc for this thread already exists, `get` it and re-publish that slug with the
handoff state folded in — with `base_rev` set to the rev you just read, or the write is refused
with `base_rev_required` — instead of minting a duplicate. Prefer `patch_doc` for the fields that
actually move (status, next_steps, blockers): it carries no body, so nothing can be truncated on
the way. Surface the returned `url` to the user, then tell them it's safe to `/compact` or step
away.
