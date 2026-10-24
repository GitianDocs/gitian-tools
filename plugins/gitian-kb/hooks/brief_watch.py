#!/usr/bin/env python3
"""brief_watch.py -- PreToolUse + PostToolUse hook: remember that the primary briefed kb-scribe.

Invoked by brief-watch.sh (matcher "Agent|Task|SendMessage") before AND after every subagent
dispatch and every message to one. Before, because a FOREGROUND scribe publishes while its dispatch
is still running, and those publishes need a brief to confirm; after, because only the returned
dispatch carries the scribe's agent id. Both record the brief the same way, and both note which
pull requests the brief names (`#N`, `/pull/N`), so a merge the agent saw and wrote up is covered
even when the poll only detects it later (ledger._merge_stamp). A compliant agent hands the scribe the activity it is about to forget, and the
nudge layer has to know WHEN it did, because that brief is the checkpoint everything later is
measured against (ledger.py: a brief is PENDING cover for BRIEF_GRACE, confirmed by the scribe's
publish). The scribe's own publish lands minutes later, from a sidechain transcript nobody here can
read; without this hook a background brief would look like silence while the scribe works, and the
next checkpoint would nag about work the agent had already handed over -- the one thing a
compliant session must never see (tests/test_invariant.py).

What counts:

  - Agent (older name: Task) with `tool_input.subagent_type` of `kb-scribe`, or ending `:kb-scribe`
    (the plugin-namespaced spelling): ledger.mark_brief. The dispatched agent's id, when the
    response carries one, is remembered with it so a later SendMessage to that agent is known for
    what it is.
  - SendMessage whose `to` is a scribe id the ledger already holds: ledger.note_scribe_message. The
    single-flight rule sends a follow-up on the same item to the LIVE scribe, and that follow-up is
    a brief too. A message to anyone else is nothing, and so is a dispatch of any other agent.

The id is found by looking, not by contract: the Agent tool's `tool_response` shape is not
documented, so a dict is asked for its `agentId` key first and every string inside it is searched
for an `agentId: <id>` rendering (the text the model itself reads). Only 6-64 characters of
[A-Za-z0-9_-] count as an id; no id still records the brief, it just cannot be messaged later.

Silent ALWAYS: this hook prints nothing, whatever it sees. It also ignores a payload carrying
`agent_id` -- a subagent dispatching or messaging another is not the primary handing work over, and
its own hooks fire under the parent's session id, so counting it would cover activity nobody
briefed.

Fail-open, always: bad stdin, a missing session id, a corrupt or unwritable state file or any other
error is swallowed by the top-level guard below and the process exits 0 having printed nothing.
Every other tool name returns before the state file is even read.

Run directly: python3 brief_watch.py < post_tool_use_envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_brief_watch.py (drives it via brief-watch.sh, end to end).
"""

import json
import re
import sys

# brief_watch.py always runs as a script file (never `python3 -c ...`), so Python has already put
# its own directory at sys.path[0] -- `import ledger` resolves the sibling module directly (the same
# convention harvest.py documents).
import ledger as ledger_mod

DISPATCH_TOOLS = ("Agent", "Task")
SCRIBE_AGENT = "kb-scribe"

_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,64}")
# `agentId: a1b2c3` as the model reads it, or `"agentId": "a1b2c3"` inside JSON text (escaped
# quotes included, and a key right after an escaped newline, as in a response that arrives as JSON
# text). A word that merely ends in "agentId" is not the key. The id must end at a non-id
# character, so a 70-character token is refused rather than cut to 64.
_TEXT_ID_RE = re.compile(
    r"(?:(?<![A-Za-z0-9_])|(?<=\\[nrt]))agentId\\?[\"']?\s*[:=]\s*\\?[\"']?"
    r"([A-Za-z0-9_-]{6,64})(?![A-Za-z0-9_-])"
)
# A response is a dict, a list of content blocks or a string; this bounds the walk should it be
# something else entirely.
MAX_WALK_DEPTH = 6


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


def _is_scribe_type(subagent_type):
    return isinstance(subagent_type, str) and (
        subagent_type == SCRIBE_AGENT or subagent_type.endswith(":" + SCRIBE_AGENT)
    )


def _agent_id(value, depth=0):
    """The dispatched agent's id from a tool_response of unverified shape, or None. A dict's own
    `agentId` wins over anything quoted deeper inside it (the scribe's report could mention another
    agent), then every string in the structure is searched for the rendered `agentId: <id>` text."""
    if depth > MAX_WALK_DEPTH:
        return None
    if isinstance(value, str):
        match = _TEXT_ID_RE.search(value)
        return match.group(1) if match else None
    if isinstance(value, dict):
        direct = value.get("agentId")
        if isinstance(direct, str) and _ID_RE.fullmatch(direct):
            return direct
        children = list(value.values())
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = _agent_id(child, depth + 1)
        if found is not None:
            return found
    return None


def _known_scribe_ids(session):
    """The scribe ids the ledger holds, read from an unlocked snapshot (a point-in-time read for a
    yes/no needs no lock, and a SendMessage to anyone else must not cost a write)."""
    led = session.get("ledger")
    ids = led.get("scribeIds") if isinstance(led, dict) else None
    return ids if isinstance(ids, list) else []


def main():
    payload = _parse_stdin()
    if payload.get("agent_id"):
        return  # a subagent's dispatch is not the primary handing work over -- see the docstring

    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}

    if tool_name in DISPATCH_TOOLS:
        if not _is_scribe_type(tool_input.get("subagent_type")):
            return
        agent_id = _agent_id(payload.get("tool_response"))  # None at PreToolUse: no response yet
        text = tool_input.get("prompt")
        now = ledger_mod.utcnow()
        ledger_mod.mutate_session(
            sid, lambda session: ledger_mod.mark_brief(session, now, agent_id, text)
        )
    elif tool_name == "SendMessage":
        to = tool_input.get("to")
        if not isinstance(to, str) or to not in _known_scribe_ids(ledger_mod.read_session(sid)):
            return
        text = tool_input.get("message")
        now = ledger_mod.utcnow()
        ledger_mod.mutate_session(
            sid, lambda session: ledger_mod.note_scribe_message(session, now, to, text)
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit, never output
    sys.exit(0)
