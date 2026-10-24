---
description: Show the gitian-kb nudge layer status
allowed-tools: Bash(python3:*)
---

Run the nudge-layer status command:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/hooks/state.py" status
```

The output shows:

- **Per-server**: vocab revision, harvest age, topic count, any undescribed topics
- **Last publish/append times**: when the KB was last written (a `publish_*`, a `patch_*`, or an `append_entry`)
- **Current session**: epoch, counters (gitian reads, edits, publishes), one-time flags already fired
- **Ledger** (lines under the session): the activity no KB write covers yet, by repo, with the time it counts from; the pull requests being watched for a merge; how long ago the last covering KB write, scribe brief and checkpoint nudge were
- **Total sessions**: how many sessions the nudge layer is tracking

**What the counters count.** `publishes` counts writes that actually landed: a call the server refused (`rev_conflict`, `base_rev_required`, `edit_no_match`, `validation_failed`, …) and a no-op republish that stored nothing (`unchanged: true`) both leave it where it was, and leave the ledger's cover where it was — so a session that only *attempted* to publish keeps its activity open for the next checkpoint nudge. `gitian reads` counts reads of the KB itself; reading a `gitian-kb://format/*` doc is reading the publish-format instructions, not the KB, so it does not count toward orientation.

If the output is empty or the state file does not exist, the nudge layer has not yet made observations — this is normal. The cache fills passively from gitian-kb MCP traffic during your session, so observations accumulate as you work.

**Delegated work counts here too.** A background `kb-librarian` or `kb-scribe` makes its gitian-kb MCP calls under *this* session's id, so its reads and publishes land in these same counters — which is what keeps the orientation check and the checkpoint nudges quiet when an agent did the work rather than you. A high `gitian reads` with nothing you called yourself is the librarian's sweep; `publishes` moving without a write in your own transcript is the scribe. The ledger works the same way: an implementer's commits land in it, and a `kb-scribe` dispatch is a brief that covers activity for 30 minutes until its publish confirms it.

**Reading the ledger.** A merge appears only after a Stop poll (`gh`, at most every 5 minutes) saw a watched PR merged, so *watching for a merge: nothing* or a missing `gh` means no merge lines, not no merges. Commits, merges and releases count on their own, file edits from three (Bash `sed -i` and redirects included); a standing rule is listed but never triggers a nudge alone; a nudge fires only when something listed is newer than the last checkpoint nudge.

The status is read-only and never modifies the state.
