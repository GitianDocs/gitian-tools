# gitian-docs

Keeps `@gitian` annotations and paired `docs/` files in sync as code changes. Ships as a skill
plus a lightweight PostToolUse hook — no MCP server, nothing to authenticate.

## Install

```
claude plugin marketplace add GitianDocs/gitian-tools
claude plugin install gitian-docs@gitian-tools
```

## What it does

- **Skill** (`skills/gitian-docs`): the discipline for keeping annotations and docs honest —
  when to update an annotation, when to add one, when to remove one, when to sync a paired doc —
  plus a condensed syntax reference and a pointer to the canonical spec
  (`https://gitian.dev/gitian-adoption-prompt.md`).
- **Hook** (`hooks/gitian-docs-nudge.sh`, wired via `hooks/hooks.json`): fires on `PostToolUse`
  after `Edit`/`Write`/`MultiEdit`. It's fast and silent by default — it only speaks up once per
  session, and only when *all* of these hold:
  - the edited file has a code extension (`.ts`, `.py`, `.go`, `.sh`, `.yaml`, …),
  - the project is gitian-instrumented (a `.gitian/` directory exists at the project root),
  - it hasn't already nudged this session (tracked via a marker file under `$TMPDIR`).

  When it fires, it adds a single-line reminder pointing at the `gitian-docs` skill — it never
  blocks or modifies the tool call.
- **Command** (`/instrument`, `commands/instrument.md`): run a gitian instrumentation pass over
  the current changes on demand — preloads the changed-files diff and the project's
  `.gitian/config.yaml` (if any), then applies the skill's duty table to those files.

## Disable it

- **Whole plugin:** `claude plugin uninstall gitian-docs` (or disable it from the plugin menu).
- **Just the hook, keep the skill:** remove or empty `hooks/hooks.json` in a local checkout, or
  disable the plugin's hooks from Claude Code's `/hooks` view — the skill still triggers on its
  own description whenever you're editing an instrumented repo.
