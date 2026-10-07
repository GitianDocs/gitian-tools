---
name: gitian-docs
description: Use when changing code in a repo instrumented with gitian (a `.gitian/` directory or `@gitian` annotations present) — keep annotations and paired docs in sync as part of the change — when adopting gitian documentation in a repo, and when you need to find or read code, a symbol or an `@gitian` annotation in a gitian-connected repository (the `code_*` tools).
allowed-tools: Bash(test:*), Bash(git diff:*), Bash(head:*)
---

# gitian-docs

## Preloaded context

- Instrumented (`.gitian/` present): !`test -d .gitian && echo yes || echo no`
- Changed files (working tree vs HEAD): !`git diff --name-only HEAD 2>/dev/null | head -20`

Preloaded so the duty pass starts from the actual change set — no tool calls needed to discover
what moved. If the changed-files list is empty or the repo isn't instrumented, there is likely
nothing to do here.

Gitian renders `@gitian` annotations and paired `docs/` markdown together as always-in-sync
documentation. That only stays true if every code change that touches documented behavior also
touches the annotation or doc describing it. This skill is the discipline for keeping the two in
lockstep — it does not teach the full syntax; fetch the canonical spec (below) for that.

## Duty table — apply on every change to an instrumented repo

| When you... | Do this |
|---|---|
| Change the behavior an existing `@gitian` annotation describes | Update that annotation's description (and metadata if the change affects it) in the same commit |
| Add a construct worth flagging (security-sensitive, a footgun, a real TODO, a new API endpoint) | Add an annotation of the right kind, right there in a comment above it |
| Remove the construct an annotation describes | Remove the annotation with it — a stale annotation pointing at deleted code is worse than none |
| Touch a file with a paired `docs/<file>.md` (or `.docs/<file>.md`) | Read the doc, update anything the change invalidated |

If none of these apply — pure refactor, formatting, a file with no paired doc and no annotations
— there's nothing to do. Don't add annotations to manufacture coverage.

## Condensed reference

**Four kinds** (set per category in config): **marker** (collapsible checklist, no code) ·
**inline** (small note card, no code) · **block** (card that captures the code directly below it —
brace/indent/JSX-aware) · **api** (Request/Response card; payloads pulled from other annotations by
id).

**Eight built-in categories** (`tags:` in config): `todo` (marker), `note` (block), `warning`
(block), `deprecated` (block), `security` (block), `bug` (block, pinned), `perf` (block), `api`
(api kind).

**Metadata** (`--key=value`, one per line, right after the annotation line): `--id=`, `--title="..."`
(display heading override), `--group=`, `--urgency=subtle|normal|loud|critical`, `--module=`,
`--request=<id>` / `--response=<id>` (api kind only — pull another annotation's captured code into
the Request/Response panel). An `--id=` is also a link target — Knowledge Base items point at it with
`[[repo:owner/name/path#@<id>]]` — so keep ids stable, unique within the repo, and lowercase-kebab.

**Docs discovery:** markdown lives in a `docs/` or `.docs/` directory at any level; `docs/foo.ts.md`
documents the sibling `foo.ts` (the docs dir's parent); `docs/payments.md` documents a sibling
`payments/` directory if one exists; anything else in a docs dir is a standalone page.

## Reading code through gitian: the `gitian-code` tools

This plugin connects gitian's **read-only code server** (`gitian-code`; the first call opens a
browser to approve "read your repositories", nothing to mint or configure). Its five tools find and
read code in a GitHub repository connected to gitian — without cloning it and without reading whole
files:

| Tool | Use it to |
|---|---|
| `code_repos` | list the repositories you can open (`owner/name`, default branch, locked or not) |
| `code_overview` | orient in one repo: modules, docs directories, whether it is instrumented, counts |
| `code_search` | find a file, doc or `@gitian` annotation by name, path, title or annotation id — never returns content |
| `code_annotation` | fetch one annotation by id: description, metadata and the code it captured |
| `code_page` | one file's outline, annotations and paired doc; an excerpt only for a named symbol, line range or `offset`/`limit` (at most 200 lines) |

The usual route is `code_repos` → `code_overview` → `code_search` → `code_page` or
`code_annotation`. Reads pass the same private-repository gate as the web viewer, so a repo you
cannot open in gitian answers `not_found`. Prefer them to guessing a path, and take only the slice
you need; never paste what they return into a document.

When a Knowledge Base item (the `gitian-kb` plugin) points at that code, it links it rather than
copying it, as `[[repo:owner/name[@ref]/path#Symbol|#Lx-Ly|#@annotation-id|label]]` — one anchor
after the path (a symbol, a line range such as `#L10-L20`, or an annotation id as `#@<id>`), then an
optional `|label` — or in the item's `code_refs` frontmatter. Take the anchor from `code_page`'s
outline or from a `code_search` hit; an annotation `--id=` is the anchor that survives edits best,
which is one more reason to keep ids stable. Better still, paste instead of assembling: a result that
names a location returns a ready-made `code_ref` (the link), `code_ref_pinned` (frozen at the commit
read) and `code_refs_entry` (the `code_refs` object; `code_overview` returns only the first two; none on any result means an older server, so write the
link by hand; none on one result means that location cannot be linked, so leave it out).
The
authoring detail (pinning a ref, the warnings) is in the `gitian-kb` plugin's authoring reference.

## Guardrails

- **Never alter runtime behavior.** This skill only adds/edits comments, `docs/`/`.docs/` markdown,
  and `.gitian/config.yaml` — no logic changes, no refactors, no unrelated reformatting.
- **Match the language's native comment syntax** (`//`, `#`, `--`, `/* */`, …) — an annotation is a
  normal comment first.
- **Signal, not noise.** Annotate the ~10-30 things a newcomer would get wrong, not everything that
  moved. Quality over coverage.
- **Lean on config defaults.** Don't invent config keys; a repo with no `.gitian/config.yaml` at all
  is already valid.

## Worked example

Behavior changed under an existing `@gitian:security` annotation — update it in place, same commit:

```ts
// @gitian:security  Validate the redirect target before issuing the session cookie.
// --id=SEC-014
// --group=auth
export function completeLogin(redirect: string, allowlist: string[]) {
  assertAllowlisted(redirect, allowlist); // was assertSameOrigin — tightened per SEC-014
  ...
}
```

The annotation's description now names the actual guard (`assertAllowlisted` against an allowlist),
not the one it replaced. No `docs/` edit needed here — this file has no paired doc.

## Canonical spec

For full annotation syntax, the `.gitian/config.yaml` schema, and adoption from scratch, fetch
`https://gitian.dev/gitian-adoption-prompt.md` — it is the canonical, always-current reference this
skill condenses. The rendered guide lives at `https://gitian.dev/guide`.
