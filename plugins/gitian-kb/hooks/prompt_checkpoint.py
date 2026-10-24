#!/usr/bin/env python3
"""prompt_checkpoint.py -- UserPromptSubmit hook: the checkpoint that costs the agent no extra turn.

Invoked by prompt-checkpoint.sh on every user prompt. A Stop block re-runs a whole turn at the
session's full context (often 300-500k tokens of cache reads), so ledger.py reserves it for
completion points; everything else -- a standing rule the owner just stated, and commits or edits
nobody wrote down for hours -- waits for the next thing the user types and rides along as
`additionalContext` on that message. The agent reads it in the turn it was going to run anyway.

Exactly three steps, in ONE locked write of the state file:

  1. `rulings.detect(prompt)`: an excerpt means the owner may have stated a standing rule. Record a
     `ruling` event (so later checkpoints list it while it is open) and, at most once per
     ledger.RULING_HINT_FLOOR, add ledger.ruling_hint(excerpt) -- one line asking the agent to fold
     a memory into the kb-scribe brief it is already writing. A ruling never blocks and never
     qualifies a checkpoint by itself: a false positive of a text heuristic must stay cheap.
  2. ledger.checkpoint_due(at_prompt=True): open qualifying activity that is new since the last
     nudge, once the quiet point is WORK_COOLDOWN old (or a completion, or the session's first
     work). Add ledger.prompt_text -- what is unwritten, and the ask for ONE batched brief -- and
     mark the ledger nudged so the same activity is not listed again.
  3. Print one JSON object, or nothing at all. When step 2 fires, its list already carries the
     rule as a `- the user:` line, so step 1's hint is left out (said once, paid for once). A compliant
     agent has briefed the scribe by the time the user types again, so its activity is covered and
     this prints nothing (tests/test_invariant.py).

Fast path: a prompt with no ruling, nothing due on an unlocked read of the state, and a transcript
already on record leaves the state file untouched. The transcript path (`ledger.transcript`) is
the one thing recorded on a quiet prompt, once per session: the /clear carry-over names it so a
scribe can be briefed from a conversation that /clear has emptied (see session_digest.py).

Silent inside a DELEGATED session (the payload carries `agent_id`, or `agent_type` for a
`claude --agent <name>` session): the nudge's remedy is "brief kb-scribe", which a subagent has no
tool for. A prompt starting with `/` carries no ruling (rulings.detect says so); the checkpoint
itself still applies to it like to any other prompt.

Fail-open, always: bad stdin, a missing session id, a corrupt or unwritable state file or any other
error is swallowed by the top-level guard below and the process exits 0 having printed nothing. The
text is only printed when the write that records it LANDED, so an unwritable state file cannot
repeat the same nudge on every prompt.

Run directly: python3 prompt_checkpoint.py < user_prompt_submit_envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_prompt_checkpoint.py (drives it via prompt-checkpoint.sh,
end to end).
"""

import json
import sys

# prompt_checkpoint.py always runs as a script file (never `python3 -c ...`), so Python has already
# put its own directory at sys.path[0] -- `import ledger`/`import rulings` resolve their sibling
# modules directly (the same convention harvest.py documents).
import ledger as ledger_mod
import rulings


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


def _can_stay_untouched(session, now, transcript):
    """Whether this prompt, with no ruling in it, can neither print nor change anything: nothing is
    due on an unlocked snapshot, and the transcript is already on record. `session` may be {} (a
    session this layer has not seen) and is normalized in place -- it is a private copy."""
    if ledger_mod.checkpoint_due(session, now, at_prompt=True):
        return False
    return transcript is None or ledger_mod.ledger_of(session).get("transcript") == transcript


def _checkpoint(session, now, excerpt, transcript):
    """Steps 1 and 2, inside the lock. Returns the context text ("" when there is none)."""
    ledger_mod.note_transcript(session, transcript)
    if excerpt is not None:
        ledger_mod.record_event(session, now, "ruling", text=excerpt)
    if ledger_mod.checkpoint_due(session, now, at_prompt=True):
        # The checkpoint text already lists the rule as a `- the user:` line, so the hint would say
        # it twice; the floor is still spent, since the agent has been told.
        if excerpt is not None:
            ledger_mod.mark_ruling_hinted(session, now)
        ledger_mod.mark_nudged(session, now)
        return ledger_mod.prompt_text(session, now)
    if excerpt is not None and ledger_mod.ruling_hint_due(session, now):
        ledger_mod.mark_ruling_hinted(session, now)
        return ledger_mod.ruling_hint(excerpt)
    return ""


def main():
    payload = _parse_stdin()
    if payload.get("agent_id") or payload.get("agent_type"):
        return  # a delegated session cannot brief kb-scribe -- see the docstring

    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return
    transcript = payload.get("transcript_path")
    transcript = transcript if isinstance(transcript, str) and transcript else None
    now = ledger_mod.utcnow()

    excerpt = rulings.detect(payload.get("prompt"))
    if excerpt is None and _can_stay_untouched(ledger_mod.read_session(sid), now, transcript):
        return

    context = ledger_mod.mutate_session(
        sid, lambda session: _checkpoint(session, now, excerpt, transcript)
    )
    if not context:
        return  # nothing to say, or the write that would have recorded it failed
    output = {"hookEventName": "UserPromptSubmit", "additionalContext": context}
    sys.stdout.write(json.dumps({"hookSpecificOutput": output}))
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit, never a partial nudge
    sys.exit(0)
