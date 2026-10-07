# gitian-docs

Keeps `@gitian` annotations and paired `docs/` files in sync as code changes. Ships as a skill
plus two lightweight hooks, and connects gitian's read-only code server (`gitian-code`) so an agent
can find and read code in a gitian-connected repository.

## Install

```
claude plugin marketplace add GitianDocs/gitian-tools
claude plugin install gitian-docs@gitian-tools
```

## Is this repo instrumented?

Both hooks answer the same question before they say anything, and stay completely silent when the
answer is no. A repo counts as gitian-instrumented when **either**:

- a `.gitian/` directory exists at the project root, **or**
- some tracked, non-markdown file contains an `@gitian:` tag.

Markdown and plain-text files (`.md`, `.mdx`, `.txt`) are excluded from that scan on purpose: a
README, a spec, or an adoption prompt that merely *documents* the syntax must not make a repo look
instrumented. `node_modules/` is excluded too. The verdict is computed once per session and cached
(see below), so the repo-wide scan doesn't run on every edit.

## What it does

- **Skill** (`skills/gitian-docs`): the discipline for keeping annotations and docs honest —
  when to update an annotation, when to add one, when to remove one, when to sync a paired doc —
  plus a condensed syntax reference and a pointer to the canonical spec
  (`https://gitian.dev/gitian-adoption-prompt.md`).
- **SessionStart hook** (`hooks/docs-context.sh`): in an instrumented repo, injects one line of
  context naming the skill by its Skill-tool id (`gitian-docs:gitian-docs`). This exists because
  Claude Code elides a skill's description from the system prompt's skill listing when the listing
  budget is tight and the skill has no usage history — a never-yet-invoked skill can be
  present-but-invisible, so the model never gets its "use when" cue. In a repo that doesn't use
  gitian the hook prints nothing at all.
- **PostToolUse hook** (`hooks/gitian-docs-nudge.sh`): fires after `Edit`/`Write`/`MultiEdit`/
  `NotebookEdit`/`Bash`. Fast and silent by default — it speaks up once per session, and only when
  *all* of these hold:
  - the tool call actually wrote code: a code-extension path (`.ts`, `.py`, `.go`, `.sh`,
    `.ipynb`, …; never `.md`) for the file tools, or — for `Bash` — a write signature (`sed -i`,
    `tee`, a `>`/`>>` redirection, `git apply`, `patch`) together with a code-file token, or a
    `git commit` on its own (commit time is when docs get reconciled). Bash coverage matters
    because in auto/accept-edits modes the model routinely edits through `sed` and heredocs, which
    an `Edit`-only matcher never sees;
  - the repo is instrumented (rule above);
  - it hasn't already nudged this session.

  When it fires, it adds a single-line reminder pointing at the `gitian-docs` skill — it never
  blocks or modifies the tool call.
- **Command** (`/instrument`, `commands/instrument.md`): run a gitian instrumentation pass over
  the current changes on demand — preloads the changed-files diff and the project's
  `.gitian/config.yaml` (if any), then applies the skill's duty table to those files.

## The code server

`.mcp.json` connects the `gitian-code` MCP server at `https://gitian.dev/api/mcp/code` (set
`GITIAN_CODE_URL=<your-gitian-host>` to point it at a self-hosted deployment). It is read-only and
carries no static `Authorization` header on purpose: the first tool call gets a `401`, your client
opens a browser to approve "read your repositories" (scope `repo:read`), and it stores the OAuth
token itself — a configured header would suppress that flow. The grant is listed under Settings →
Connected agents as **Repositories** and is revocable on its own; it is separate from the
knowledge-base connection (`gitian-kb`), and a credential for one is refused by the other.

Five tools, with the contract in `docs/code-mcp-transport.md`: `code_repos` (which repos you can
open), `code_overview` (orient in one), `code_search` (find a file, doc or `@gitian` annotation —
never returns content), `code_annotation` (one annotation by id) and `code_page` (a file's outline,
and a bounded excerpt only for a symbol or line range you name). The tools appear as
`mcp__plugin_gitian-docs_gitian-code__code_*`. A Knowledge Base item points at what they find with a
`[[repo:owner/name[@ref]/path#Symbol]]` link or a `code_refs` entry (see the skill).

## State

Per-session markers and the cached instrumentation verdict live in `~/.claude/gitian-docs/`
(`$CLAUDE_CONFIG_DIR/gitian-docs` when that variable is set), overridable with
`GITIAN_DOCS_STATE_DIR`:

- `gitian-docs-nudge-<session_id>` — the once-per-session marker for the PostToolUse nudge.
- `probe-<session_id>` — the cached `<verdict><TAB><project dir>` from the instrumentation scan,
  recomputed whenever the recorded project dir doesn't match the current one.

Files are keyed on the `session_id` Claude Code passes in the hook payload, and anything older
than 7 days is pruned on the next hook run. `/clear` (SessionStart `source=clear`) deletes the
nudge marker so the reminder re-arms for the fresh conversation.

## Tests

POSIX-sh hooks with python3 stdlib tests (no `jq`, no third-party packages), shellcheck-clean:

```
python3 plugins/gitian-docs/hooks/tests/test_nudge.py
python3 plugins/gitian-docs/hooks/tests/test_docs_context.py
```

## Disable it

- **Whole plugin:** `claude plugin uninstall gitian-docs` (or disable it from the plugin menu).
- **Just the code server:** disconnect it from `/mcp`, or revoke the **Repositories** grant under
  Settings → Connected agents.
- **Just the hooks, keep the skill:** remove or empty `hooks/hooks.json` in a local checkout, or
  disable the plugin's hooks from Claude Code's `/hooks` view — the skill still triggers on its
  own description whenever you're editing an instrumented repo.
