# gitian-tools

Claude Code plugins for [gitian](https://gitian.dev) — install the marketplace once,
then enable whichever plugins you want:

```
claude plugin marketplace add GitianDocs/gitian-tools
claude plugin install gitian-kb@gitian-tools     # knowledge base (MCP + discipline)
claude plugin install gitian-docs@gitian-tools   # documentation-sync discipline
```

Each plugin is independent — install either or both, and toggle them per-project
with `claude plugin enable/disable` or the `/plugin` menu.

## gitian-kb

Connects Claude to your gitian Knowledge Base over MCP (browser-based OAuth — no
tokens to paste) and ships the publishing discipline: query prior decisions before
starting work, publish distilled memories/docs/journal entries at natural
completion points, keep statuses terminal-true.

## gitian-docs

The code-documentation discipline for gitian-instrumented repos: keep `@gitian`
annotations and paired `docs/` files in sync as code changes, a once-per-session
nudge when you edit code in an instrumented repo, and an `/instrument` command
that runs a documentation pass over your current changes.

---

This repo is a distribution mirror; development happens in the main gitian
repository.
