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
- **Last publish/append times**: when you last published or appended to the KB
- **Current session**: epoch, counters (gitian reads, edits, publishes), nudge once-flags already fired
- **Total sessions**: how many sessions the nudge layer is tracking

If the output is empty or the state file does not exist, the nudge layer has not yet made observations — this is normal. The cache fills passively from gitian MCP traffic during your session, so observations accumulate as you work.

The status is read-only and never modifies the state.
