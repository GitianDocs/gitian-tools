# Topics, mentions, and categories

How an item joins the corpus. Doc-doc relatedness is entirely topic-derived — there's no other
correlation signal besides an explicit `related`/wikilink — so linking is not decoration. Read
`gitian-kb://vocab` (or call `read_resource` with that uri) before every publish that links topics.

## Linking rules

- **`topics`** (primary tier, "this item is *about* X") — advise 1-3 per item. **`mentions`**
  (secondary tier, "this item *touches* X" without being about it) — as many as apply. A slug in
  both collapses to primary.
- **Prefer existing topics.** `gitian-kb://vocab` lists every live topic with its description and
  degree — link to what's already there before considering a new one. An empty description means
  nobody has described that topic yet (a **stub**); it is a real, live topic all the same and links
  to it score exactly like any other. A vague, catch-all topic (or one linked to nearly everything)
  contributes almost nothing to relatedness by construction (informativeness falls as membership
  grows), so precision beats coverage.
- **Freshness discipline.** `gitian-kb://vocab` also carries `freshness` (0-1) and `dormant`
  (`freshness` below a fixed threshold) per topic, sorted freshness-descending — a topic nobody has
  linked in a while quietly decays, and any new item linking it revives it instantly, no ceremony
  required. When more than one live topic reads as on-topic, prefer the fresher one; treat the
  dormant tail as lower-priority, not gone — a dormant topic is due for a second look (re-link
  deliberately, or let it keep decaying) rather than an automatic pick. This is a display/routing
  signal only — it never changes what a link means, only which topic to reach for first.
- **Aliases resolve transparently.** A merged topic never appears as its own entry in
  `gitian-kb://vocab` — only its canonical does, carrying `aliases: [...]` for every slug now
  merged into it. A link naming an alias still works (it resolves to the canonical on every read —
  degree, relatedness, `neighbors`, `topic`), but prefer linking the canonical spelling once you
  see it in the vocab response rather than perpetuating the old name. Merge/unmerge itself is a
  human call in the review queue, not a tool call — nothing here mints or removes an alias on your
  behalf.
- **Cold domain: mint, don't default.** When nothing in `gitian-kb://vocab` fits the domain of what
  you're publishing — a genuinely new area of work the vocabulary hasn't caught up to yet — mint
  1-3 genuine concept topics with real descriptions via `publish_topic` (not a bare auto-mint left
  undescribed). Never default to a topic that just names the project or repo (see
  `project_name_topic` in `authoring.md`) and never default to publishing with empty `topics` (see
  `no_topics`) — an empty or project-name-only vocab is exactly the situation this rule exists for,
  not an excuse to skip linking.
- **A novel slug auto-mints — deliberately, not for free.** Naming a topic slug in
  `topics`/`mentions` that isn't in the vocab yet is never rejected and never inert: it auto-mints
  as an undescribed **stub** and is live in relatedness immediately, no `publish_topic` call
  required, no approval anywhere, and the response carries an informational
  `organic_topics_minted` warning naming what got minted. That lowered floor is not a license to
  invent freely — every fresh mint is a permanent vocabulary entry someone (a human, or
  `kb-librarian`'s vocab-delta reports) eventually has to make sense of, and a slug chosen
  carelessly is exactly how synonym fragmentation creeps in. Check `gitian-kb://vocab` for an
  existing slug that already names the concept before typing a new one; only mint when the concept
  is genuinely absent. `publish_topic` is how a stub stops being one: it attaches a real
  description (or refreshes an existing one's) — call it when the concept deserves documentation,
  not to make a link "count", which it never affected. The one slug family that stays inert is a
  **tombstoned** one: a user veto is never overruled by frontmatter, so the link is stored but
  excluded from relatedness (advisory `tombstoned_topics` warning) until someone deliberately
  re-mints it via `publish_topic`.
- **`category`** — at most one, `null` if none. Pick from `gitian-kb://vocab`'s categories using
  their routing prompts; an unknown category slug gets the same late-binding treatment
  (`unknown_category` warning). Categories are authored in the `/kb` UI, not minted over MCP —
  when nothing in the vocabulary fits, `null` is the answer, never an invented slug.
- **Update-over-create bias.** Before minting a brand-new `doc` slug, check whether an existing
  *active* doc already owns the same primary topics — `list({topic: "<slug>"})` or the `topic`
  tool's member list — and update that doc instead of publishing a near-duplicate. The server
  backstops this with an advisory `consider_update` warning on a rev-1 doc mint whose primary
  topics heavily overlap an existing doc's, but don't rely on the backstop catching everything;
  check first.
- **`neighbors`** on a slug returns each hit's `why.topics` (the shared topics driving the score,
  richest first) and `why.explicit` (non-null when an explicit link floors the weight at 1.0) — use
  it to understand *why* something surfaced, not just *that* it did.
- **`topic`** (hub view) and **`publish_topic`**/**`retract_topic`** (mint/update/tombstone) round
  out the toolset for working with the vocabulary directly — see their tool descriptions for the
  exact shapes.

## Who chooses topics

A brief names topic slugs **to mint** only for a genuinely new concept. Beyond that: link any
existing vocabulary topic that fits (that is ordinary linking, not a judgment call), mint only the
slugs the brief named, and when nothing in the vocabulary fits and the brief named nothing, publish
without topics and **flag it in the report** so the primary can decide. Never invent a slug to fill
a count.

## Topic extraction contract

*(Mirrors `EXTRACTION_CONTRACT` in `packages/kb/src/extract/logic.ts` verbatim, so the two modes
below can't drift on wording.)*

A doc's topic links get populated one of two ways. An owner can opt into server-side Gemini
extraction — a background pass, gated per-owner, off by default — which enriches
`kb_topic_links` asynchronously after the doc publishes. Absent that opt-in (the default), it's
on you: extract topics yourself before calling `publish_doc`.

Read the topic vocabulary first, then prefer linking to an existing topic over minting a new
one. Assign 1-3 topics as "primary" (what the doc is about) plus any number of "secondary"
topics (what it merely touches). Mint at most 2 brand-new kebab-case topics, and only when
nothing in the existing vocabulary fits. Never re-mint a topic a human has retracted
(tombstoned) — that veto stands even when the topic would otherwise be a perfect fit.

If a `publish_doc` response carries `doc_without_topics`, the doc landed with empty
`topics`/`mentions` and the owner isn't on server-side extraction — apply the contract above
(read `gitian-kb://vocab`, link 1-3 existing primaries, mint at most 2 new ones) and re-publish
the same slug. This is the same linking discipline **Linking rules** above already asks for on
every publish; the warning only fires when a topic-less doc slips through anyway.
