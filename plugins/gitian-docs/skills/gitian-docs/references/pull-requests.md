# Pull requests through gitian

`code_prs` and `code_pr` read a **mirror** of a repository's pull-request metadata (titles,
branches, logins, labels, states, sizes, commit headlines, file paths, review states), kept for the
repositories where the gitian GitHub App is installed with pull-request sync on. Never a pull
request's body, comments, review text or patch. The contract is `docs/code-mcp-transport.md`.

## `code_prs`: which pull requests

`repo` (required) plus any of: `state` (`open`, `closed`, `merged`, `all`), `author`, `label`,
`head` (the exact source branch), `updated_since` (an ISO date or timestamp), `limit` (1-100,
default 25) and `cursor` (the previous `next_cursor`). Newest-updated first.

- **"Is someone already changing these files?"** `state: "open"` with `path` set to the repo-relative
  file. Ask once per file you are about to change; a hit is a reason to look at that pull request
  (`code_pr`), not a verdict. `path` matches only pull requests whose files are mirrored (open
  ones, and those updated in the last 30 days).
- Rows of a pull request whose detail is not mirrored yet carry null sizes and review decision.

## `code_pr`: one pull request

`repo`, `number`, and `include`, any of `commits`, `files`, `reviews`, `timeline`. Omit `include` for
the card alone; a call costs what it asks for. Sections come beside `pr`. A file's `code_ref` is
pinned: the merge commit once merged, the head commit before that. A list that hit its cap says so
(`commits_truncated`, `files_truncated`, `reviews_truncated`).

## Paste the `pr_ref`

Each row and card carries `pr_ref`, the complete wikilink, brackets included (`[[pr:owner/name#N]]`).
Paste it into a KB item's body instead of typing the grammar (the `gitian-kb` plugin's authoring
reference, *Referencing pull requests*). A pull request with no `pr_ref` cannot be linked: leave it
out. A reference carries no state; say what the pull request changed in a sentence of your own.

## Freshness

Every row has `synced_at`, and `code_prs` also the repository's last finished sync and
`backfill_complete` (`false`: an old pull request may be missing). Relay state as "as of
`synced_at`". `indexing` (with `retry_after_s`) means the mirror is catching up: retry, never read
it as "no pull requests". `not_found` covers a repository that is not mirrored or not visible to you.
