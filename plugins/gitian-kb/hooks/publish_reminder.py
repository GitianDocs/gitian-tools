#!/usr/bin/env python3
"""publish_reminder.py -- the gitian-kb nudge layer's Stop hook: the completion checkpoint.

Invoked by publish-reminder.sh (stdin passed straight through, unread by the wrapper) on every Stop
event. It asks the session's activity ledger (ledger.py) one question -- "has the session reached a
completion point that no KB write covers yet?" -- and answers it with at most one block per
completion: {"decision": "block", "reason": ...}, whose reason lists, repo by repo, what is
unwritten, so the agent can brief kb-scribe ONCE with all of it instead of reconstructing hours of
transcript.

Why the Stop hook is the place for completion points ONLY. A block is not a message, it is a whole
extra turn: the model re-runs at the session's full context (often 300-500k tokens of cache reads)
and the brief it then writes is a ~100k-token scribe run on top. So this hook spends that only where
nothing cheaper exists -- the session's first work, and a merge or release (at most one per
COMPLETION_FLOOR, so a burst of merges is one block). Commits, edits and standing rules ride along
as a few lines of context on the user's next prompt (prompt_checkpoint.py) or at SessionStart, which
cost no turn. `ledger.stop_decision` encodes that split and this module never second-guesses it.
It also fires only at a CHECKPOINT, never right after an event: a compliant agent has briefed the
scribe by the end of its turn, so its activity is already covered and the session sees nothing --
the release gate in tests/test_invariant.py.

Why it polls. A merge happens ON GitHub: `gh pr merge --auto` only ARMS one, and the real merge
lands when CI goes green or a person clicks, long after any command a hook could see. So the
ledger keeps a WATCH list of pull requests the session opened or set merging, and this hook sends
the due ones to pr_poll.py -- one `gh api graphql` call, at most every POLL_INTERVAL. MERGED becomes
a `merge` event (the only source of merges), CLOSED is dropped, anything else keeps being watched.

Why the state lock is never held across `gh`. The lock serializes every hook of every session on the
machine, and the poll is a network call (a 4 s cap against this hook's 10 s budget; a hung `gh`
costs the whole cap). So the order is: read an UNLOCKED snapshot, decide from it what is due, make
the call with no lock held, and only then take ONE locked read-modify-write that folds the answer
in, makes the decision AGAIN on the state as it is now (a racing Stop or another hook may have got
there first -- this is what keeps a nudge to one) and stamps it shown. A turn end with nothing due
and nothing to say returns before it ever takes the lock, and writes nothing.

Silent -- no poll, no write, no output -- when the payload carries `stop_hook_active` (the loop
guard: this Stop continues an earlier block; checked first, unconditionally), `agent_id` (a
DELEGATED session cannot brief kb-scribe, the only remedy on offer; Claude Code fires SubagentStop
rather than Stop for a subagent today, so this guards a payload shape, not a path known to run, and
the cost of being wrong the other way is a blocked turn) or no session id.

The transcript is never read here. Its path is only remembered (ledger.note_transcript) so a
/clear can name it to the scribe: a turn end costs a state read, not a scan of a transcript tens of
MB long.

Fail-open, always: a missing `gh`, corrupt state, a failed write or any other error is swallowed
by the top-level guard in `__main__` and the process exits 0 having printed nothing. The block is
printed in one write and only AFTER the write that records it succeeded: a nudge that could not be
remembered is never shown, because it would repeat at every turn end.

Run directly: python3 publish_reminder.py < stop_envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_publish_reminder.py (drives it via publish-reminder.sh,
end to end; a fake `gh` first on PATH answers the poll).
"""

import copy
import json
import sys

# publish_reminder.py always runs as a script file (never `python3 -c ...`), so Python has already
# put its own directory at sys.path[0] -- the sibling imports below resolve directly, no path
# manipulation needed (matches harvest.py's own house style).
import ledger as ledger_mod
import pr_poll


def _parse_stdin():
    """Read the whole hook-input envelope. Unparsable/non-object stdin -> {}."""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    return payload


def _scratch(session):
    """A copy for the decision functions to normalize freely: ledger.due_watch trims the watch and
    the readers reshape whatever dict they are handed, and the snapshot is read twice."""
    return copy.deepcopy(session)


def main():
    payload = _parse_stdin()

    # Loop guard -- ALWAYS first, unconditionally. Truthy, not `is True`: the cost of a stray value
    # reading as "active" is one skipped nudge, the cost of the other way round is a block loop.
    if payload.get("stop_hook_active"):
        return
    if payload.get("agent_id") or payload.get("agent_type"):
        # A subagent (agent_id) or a `claude --agent <name>` session (agent_type, the field
        # SessionStart documents) cannot brief kb-scribe -- see the module docstring.
        return

    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return

    now = ledger_mod.utcnow()
    snapshot = ledger_mod.read_session(sid)  # unlocked: it only decides whether to ask GitHub
    due = ledger_mod.due_watch(_scratch(snapshot), now)
    results = pr_poll.poll(due) if due else {}  # the network call -- NO state lock held here
    if not due and not ledger_mod.stop_decision(_scratch(snapshot), now):
        return  # nothing to ask and nothing to say: a quiet turn end writes nothing

    transcript = payload.get("transcript_path")

    def checkpoint(session):
        # Everything below is judged on the state as it is NOW, under the lock. The clock is read
        # again because the poll may have taken seconds, and `nudges.at` must not be older than
        # activity another hook recorded meanwhile: that activity is in the text shown below, so it
        # must not count as new at the next checkpoint.
        at = ledger_mod.utcnow()
        ledger_mod.note_transcript(session, transcript)
        if due:
            ledger_mod.apply_poll(session, at, results, polled=due)
        if not ledger_mod.stop_decision(session, at):
            return None
        reason = ledger_mod.stop_reason(session, at)
        ledger_mod.mark_nudged(session, at)
        return reason

    reason = ledger_mod.mutate_session(sid, checkpoint)
    if reason:
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit, never a partial nudge
    sys.exit(0)
