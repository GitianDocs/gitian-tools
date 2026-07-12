# gitian-spec

Author long-form work documents — specs, plans, designs, brainstorms, handoffs, session notes —
as gitian Knowledge Base docs instead of loose markdown files. "Write me a design doc for X"
produces a published KB doc with a full manifest and a URL, not an untracked file in the repo.

## Install

```
claude plugin marketplace add GitianDocs/gitian-tools
claude plugin install gitian-kb@gitian-tools    # required companion — provides the MCP connection
claude plugin install gitian-spec@gitian-tools
```

## Requires gitian-kb

This plugin deliberately ships **no MCP server config**. The `gitian` tools it authors through
(`search`, `neighbors`, `get`, `history`, `file_intents`, `publish_doc`, `publish_entry`) come from the gitian-kb
plugin's single connection — so installing both never double-connects the same server, and schema
authority, OAuth, and the publishing rules live in exactly one place. Without gitian-kb the
skill's discipline still loads, but there are no tools to publish with; the SessionStart hook
detects the gap and says so.

## What it does

- **Skill** (`skills/gitian-spec`): the authoring discipline — scan the KB before writing
  (search + neighbors, adopt settled decisions, cross-link, flag stale manifests), choose the
  right shape (doc vs entry, type mapping for brainstorms and progress updates), derive every
  manifest field from ground truth (project from cwd, branch/worktree from git, dates from the
  clock), terminal-state hygiene, commits-list conventions, and the 14-point implementation-recap
  checklist.
- **Hook** (`hooks/spec-context.sh`, wired via `hooks/hooks.json`): fires on `SessionStart`. One
  routing line (long-form docs are KB deliverables); it adds nothing gitian-kb's session hook
  already injects, and warns only when gitian-kb is missing from `installed_plugins.json`.
- **Command** (`/gitian-spec:recap`, `commands/recap.md`): run at feature-landing time — flips
  the governing doc's manifest to its terminal state and writes the implementation recap, with
  the recent `git log` preloaded.

## Boundaries with gitian-kb

gitian-spec owns doc-as-deliverable **authoring** (being asked to write a spec/plan/design/
brainstorm/handoff/session note; documenting what a landed feature shipped). gitian-kb owns
**orientation** (RAG at work-start), completion-point distillation, the journal, and memories.
Both SKILL.md files cross-reference each other, and gitian-spec inherits gitian-kb's publishing
rules by reference — the `gitian-kb://format/*` resources stay the single schema authority.

## Disable it

- **Whole plugin:** `claude plugin uninstall gitian-spec` (or disable it from the plugin menu).
- **Just the hook, keep the skill:** disable the plugin's hooks from Claude Code's `/hooks` view —
  the skill still triggers on its own description whenever long-form doc authoring comes up.
