# Targeting a KB (`kb`), linked KBs, org KBs, and `vocab_rev`

Which KB a call lands in, how a hit labels itself, and how to stay in sync when the vocabulary
moves mid-session. Read this before a write into anything other than `home`, and whenever a
response carries a `kb` label you didn't pass in.

## Targeting a KB (`kb`)

Every tool takes an optional `kb` alongside its other arguments — you can belong to more than one
KB (your `home` KB, any custom ones you're a member of, and any **org** KB you hold a seat for),
and others can be linked to you read-only. Two forms are accepted: a bare slug for a KB of your
own, or a qualified `login/kb-slug` — which addresses both a KB linked to you (see **Linked KBs**
below) and an org KB, whose `login` is the org's (see **Org KBs** below). Most sessions never need
to set it: a write with no `kb` lands in `home`, and a bare slug on a single-target read
(`get`/`history`) falls through to the rest of your KBs only if it isn't found in the default. Pass
`kb` explicitly whenever the work belongs to a KB other than `home` — don't assume a `kb` you
passed on one call carries forward to the next; **every call is independent, and nothing binds a KB
for the rest of a session**. A scribe's brief always states a `kb` — a label, or the literal `auto`
meaning "route on `repo`" — and `auto` with no `repo` can only ever land in `home`.

- **When to pass it.** Sweeping reads (`search`/`list`/`neighbors`/`file_intents`) sweep every KB
  you belong to *plus* any KB linked to you (see **Linked KBs** below) by default, each hit labeled
  with the KB it came from — pass `kb` only to narrow to one you already know you want. Writes
  (`publish_*`/`patch_*`/`append_entry`/`retract_item`/`publish_topic`/`retract_topic`) and the
  single-target reads (`get`, `history`, `topic`) default to `home` — pass `kb` whenever you're
  working inside a different one, on every call, not just the first. `get`'s `owner` argument is
  **deprecated and ignored** — it is still accepted so older clients don't break, but it grants
  nothing: a teammate's org work lives in an **org KB** you are a member of (see **Org KBs**
  below), so a plain `get` reaches it, and a bare slug that isn't in any KB you can read is
  `not_found` whatever `owner` says.
- **`ambiguous_slug`.** A bare slug (no `kb` given) to `get`/`history` that exists in more than one
  KB you can read — your own, linked, or **your own items in a write-only org KB** (`own-only`, see
  **Org KBs** below; a teammate's slug there is never a candidate, and never counts toward the
  ambiguity), beyond your default KB, which always wins the tie and never counts as ambiguous —
  comes back as an `ambiguous_slug` error naming `candidates` (`{ kb, primitive }` pairs, drawn only
  from KBs the slug actually matched). Retry the *exact same call* with `kb` set to the candidate
  you mean, copying its `kb` value exactly (a linked candidate's is qualified — see **Linked KBs**
  below). Never guess which one by picking the first candidate, by title, or by recency — the error
  exists precisely because that guess is unsafe.
- **`reserved_kb_slug`.** Not a tool-call error — creating a KB, custom or org (the `/kb` UI's
  create flow and the org KBs panel at `/settings/org`; there is no MCP tool for either), rejects a
  requested slug that collides with a fixed route (`home`, `memory`, `doc`, `entry`, `graph`,
  `linked`) with this code. If you're ever asked to help name a new KB, steer clear of those six.

## Linked KBs (`login/kb-slug`)

Someone else's KB can be *linked* to one of yours — a **read-only**, **one hop**, **mutual**
grant the two sides agree to through an invite. Once it's accepted, that KB's items show up in
your sweeping reads alongside your own — *and yours show up in theirs*. A link is not a one-way
"I get to read them": accepting exposes the whole KB you accept into, which is why accepting a
link invite needs the `admin` role on that KB, the same role needed to send one. If a user asks
whether to accept, say that plainly — the exposure is the decision. Nothing about writing
changes either way: `publish_*`, `patch_*`, `append_entry`, `retract_item`, `publish_topic` and
`retract_topic` can only ever land in a KB you're a member of. Inviting, accepting, declining,
and unlinking all happen in the web UI (the `/kb` invites inbox and `/kb/<kb>/settings`) — there
is **no MCP tool for inviting or linking**, and no tool that lists who is linked to you. Don't
try to arrange one over MCP; ask the user.

- **Anything you don't own labels itself qualified.** The split is ownership, not access tier:
  hits from a KB *you own* carry a bare `kb: <slug>`, and every other hit — a linked KB, a KB
  someone invited you into, an org KB — carries `kb: <login>/<kb-slug>` instead.
  **Pass that label back verbatim** as the `kb` argument on any follow-up call — `get`, `history`,
  `neighbors`, a narrowed `search`; the qualified form addresses all three, on reads and (where
  you have write access) writes. Never strip the `login/` prefix and retype the bare slug: a bare
  slug prefers a KB you *own*, and since every user has one slugged `home`, the bare retry usually
  resolves somewhere real and wrong rather than failing loudly.
  (`file_intents` is the one exception to the label rule: its linked hits carry no `kb` at all —
  they're identified by `owner: {login}` plus a `/kb/linked/<login>/<kb-slug>/...` url, and that
  url's `<login>/<kb-slug>` is what you pass as `kb` to follow one up.)
- **Two KBs you belong to can share a slug.** Slugs are unique per owner, not globally, and a
  *member* invite — or an org KB — can put someone else's KB alongside your own same-named one
  (`home` especially). A bare `kb` resolves **own-first**: your own wins the tie silently, and if
  *neither* is yours it resolves to nothing rather than picking. Either way the qualified label is
  the unambiguous address — use it whenever it matters which one you mean, rather than inferring
  from a hit.
- **Never guess across ambiguity.** A bare slug matching no KB of your own but two or more linked
  KBs resolves to nothing — the same masked `not_found` an unknown KB gets, not a pick. When
  `ambiguous_slug` comes back, its `candidates` already carry the qualified `kb` values: retry
  with the one you mean, copied exactly. Choosing by order, title, or recency is precisely the
  guess these errors exist to prevent.
- **One hop, never transitive.** A KB linked to a KB that's linked to yours is not yours to read,
  and no argument makes it so. Nor does a link ever widen to a KB's own linked set. If something
  you expected isn't in the sweep, that's the boundary working — say so and ask for a link rather
  than hunting for a targeting trick.

## Org KBs (`org-login/kb-slug`)

**Every gitian org has one**, slugged `team` — `<org-login>/team` — and it exists from the moment
gitian first sees the org. It behaves like any other KB you're a member of (swept by default,
addressed by the same two `kb` forms) with two things that have no analogue elsewhere:
**membership is derived, never granted** — being in the GitHub org is the whole of it, there is no
invite to accept and nothing to revoke — and **write and read are separate grants**, below.

- **Writing is free; reading a teammate's work is the org's subscription.** Any member of the
  GitHub org can publish into its `team` KB whether or not the org pays for anything. Until the org
  subscribes you read back **exactly what you wrote there and nothing else** — your own items come
  through every sweep, labeled `kb: <org-login>/team` like any other hit, and the KB is named in
  `own_only_kbs` so a thin result is never mistaken for "the team has nothing in flight". A
  `get`/`history` **naming that KB** (`kb: <org-login>/team`) on a slug you didn't write answers
  `read_requires_entitlement` — "'<kb>' shows only what you wrote until the org subscribes;
  '<slug>' is not yours" — rather than a masked `not_found`, because your write grant already
  proves the KB and the slug exist. A **bare** slug is different: it never resolves to a
  teammate's item there at all, and is not reported as an `ambiguous_slug` candidate either, so the
  bare form is not an existence oracle — it just answers `not_found`. Nothing is lost and nothing
  needs re-doing; the org subscribing makes all of it readable at once, with nothing to migrate.
  Say that plainly rather than re-publishing the work somewhere else.
- **Duplicates are the expected cost of an unpaid org.** Two members can write near-identical docs
  without either seeing the other's, and a slug you cannot read is never a reason to mint the same
  work under a different name. Write yours; once the org subscribes the pair becomes visible and
  gets reconciled — that reconciliation is the **dedupe run** (`kb-scribe`, per the dedupe section
  of `authoring.md`), and picking which of the two survives is the primary's call, never the
  subagent's.
- **Docs AND journal entries about an org's repos route there by default.** A `publish_doc`,
  `publish_entry` or `append_entry` with **no `kb`** whose `repo` is `<org>/<name>` (the owner
  matched case-insensitively) lands in `<org>/team`, not in `home`. **Memories never route** — a
  `publish_memory` stays in `home` whatever its `repo` says. The response says where it went in
  `routed_to`, and carries `kb_read_paywalled` when that KB is one you can write but not read. An
  explicit `kb` always wins — routing is the default, never an override — and a `kb` that doesn't
  resolve answers `not_found` rather than quietly falling back to `home`.
- **Every successful write answers `landed_in`.** It is the label of the KB the item is actually in
  (`home`, `<org>/team`, `login/kb-slug`). Read it on every write, report it, and never assume
  `home`: a write with neither `kb` nor `repo` has nothing to route on and lands in `home`, which in
  an org repo is how a team's journal gets forked silently. That case has its own alarm — a kb-less
  **and** repo-less `publish_doc`/`publish_entry`/`append_entry` that CREATES an item in `home` while
  you belong to a routable org carries `repo_missing_cannot_route`, since routing is computed from
  `repo` alone: **no `repo`, no routing**. A create with no `kb` that lands in `home` while the same
  slug exists in another KB you can write also carries `slug_exists_in_other_kb` — the forked-work
  alarm; report either one rather than republishing anything.
- **A routed publish creates; it never revises a body it didn't write.** If the slug already
  exists in the routed KB, the write is refused with `slug_taken` rather than silently rewriting a
  teammate's doc — the error's own hint says so: pass `kb: <org>/team` **and** `base_rev` (the rev
  you read) to revise it deliberately. Both halves are required: the `kb` says which KB you meant,
  the `base_rev` says which revision you built on (see **Revising an existing item** in
  `authoring.md`). A `patch_doc` with no `kb` finds the doc in the routed KB that holds it (the
  response says `routed_to`); it is never refused with `slug_taken`, though it still needs its own
  `base_rev`. Only `publish_doc` needs an explicit `kb` to revise a doc that already exists in the
  team KB.
- **Two per-member toggles, both the org admin's.** Auto-routing can be turned off for you (then
  the item stays in `home` and you get an advisory `org_kb_available` warning naming the KB to
  re-publish into — that warning also fires on the primitives routing never claims, a memory or an
  unrouted entry), and write access itself can be turned off (then you read the KB your seat pays
  for and publish nothing into it). Neither is yours to change; they live in the org's settings on
  the web. **An explicit `kb` silences `org_kb_available`**: both routing advisories are about
  **kb-less** landings only, so passing the target you mean is also how you stop being advised about
  it.
- **Address it qualified.** `team` is every org's slug, so a bare `team` names none of them for
  anyone in two orgs — and if it collides with a KB of your own, yours wins. `<org-login>/team` is
  the address, on reads **and** writes, and it is the form reads label hits with. Pass that label
  back verbatim, same as a linked KB's.
- **Team work belongs in the org KB.** A doc or entry published into your own `home` KB about an org
  repo is **invisible to your teammates** — nothing widens across personal KBs. Publish shared
  plans, specs, designs and the team journal into the org KB (routing does this for you unless it is
  switched off); keep personal notes in `home`. Memories never route, and "move it there" is
  guidance for shared work, not for a personal note.
- **`get`'s `owner` argument is dead.** It used to reach a teammate's doc in their personal KB
  under an org-derived grant. There is no such grant now: read the org KB.
- **Membership is not something you can arrange.** Org membership and seats live in the org's own
  settings on the web, not over MCP. If a KB you expected isn't in your sweep, say so — don't hunt
  for a targeting trick.
- **The org's categories are its admin's.** Every member writes items and mints topics; only the
  org's admin adds or renames the `category` vocabulary. Pick from what `gitian-kb://vocab` lists.

## Staying in sync mid-session (`vocab_rev`)

Every successful tool response carries `vocab_rev` — a counter scoped to the **target KB** (the
`kb` a write/single-target read resolves to, or your default `home` when omitted) that bumps on
any vocabulary write to that KB: category CRUD, a topic mint/describe/tombstone/merge, including
an auto-mint that happened as a side effect of someone else's publish. Track the value you last saw
*per KB* — a `vocab_rev` from a custom KB and one from `home` are different counters and never
comparable to each other. **If a later call's `vocab_rev` differs from the one you last saw for
that same KB, re-read `gitian-kb://vocab` before your next publish into it** — the vocabulary
changed mid-session, and publishing against a stale read risks linking a slug that no longer means
what you think, minting a near-duplicate of something a teammate (or an earlier call in this same
session) just minted, or missing a category that now fits. This is cheap: the vocab resource is
small and the freshness signal rides on calls you're already making — no polling, no extra round
trip. One catch: `gitian-kb://vocab` is a static URI with no per-read `kb` argument, so a resource
read always serves your **default** KB, which is always `home` (nothing binds another) — a session
working a non-default `kb` sees that KB's `vocab_rev` on every response but has no resource read
that reflects it, so don't chase that counter against `gitian-kb://vocab`; there's nothing yet to
re-read it against. (The `read_resource` tool takes a `kb`, so an agent reading the vocab that way
can target the KB it is writing into.) When the drift is more than "one topic changed" in your
default KB, dispatch `kb-librarian` for the vocab-delta refresh instead of re-reading and
re-diffing it yourself.
