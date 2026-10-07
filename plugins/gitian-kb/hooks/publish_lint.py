#!/usr/bin/env python3
"""publish_lint.py -- PreToolUse publish lint for the gitian-kb plugin's nudge layer.

Invoked as a single python3 process (stdin passed straight through, unread by publish-lint.sh) by
publish-lint.sh, itself registered as a PreToolUse hook matching
"mcp__(plugin_gitian-kb_)?[gG][iI][tT][iI][aA][nN]__(publish_doc|publish_memory|publish_entry|append_entry|patch_doc|patch_memory|batch_write)"
-- the KB server's namespace (kb_tool.py: both spellings, case-insensitive on the server segment),
never the code server's -- the patch tools included, since a patch revises an item's frontmatter and its topic/mention lists
are exactly what this lint is about, and batch_write, which carries up to 25 of those six writes
in `operations[{tool, args}]` and so is where a bulk import's typos would otherwise go unseen.
`publish_category` and `publish_topic` are not linted: they carry no topic/mention lists.

It NEVER BLOCKS. The advice travels as PreToolUse `hookSpecificOutput.additionalContext` with no
`permissionDecision` at all, so the call proceeds through the normal permission flow untouched and
the advice reaches the model next to the tool's result (Claude Code hooks reference, "Add context
for Claude": PreToolUse's reminder appears "next to the tool result"; and "staying silent doesn't
approve it" -- omitting the decision is not an `allow`, which this hook must never emit, since an
`allow` would skip the user's own permission prompts). It used to answer `deny` once per rule per
session and rely on the model re-sending the identical call; delegated writers read that as a
refusal and stopped, so an "advisory" lint blocked real publishes.

Guard: tool_name must be a tool of the gitian KB server (kb_tool.is_kb_tool, the harvest.py
convention); anything else -- another server's, the code server's -- is silent. The matcher above
is only a coarse pre-filter -- this guard is the real gate.

One rule, the one the server cannot see. Two earlier rules are gone because the server already
returns them as warnings in the very tool response this advice now sits beside: empty topics
(the server's `no_topics` / `doc_without_topics`, with `suggested_topics`) and a topic equal to the
project or repo name (the server's `project_name_topic`). Repeating them here only doubled the
noise.
  near-miss (flag lint_near_miss) -- any slug in tool_input.topics + tool_input.mentions that is
     NOT an exact cached slug but sits within Levenshtein distance 2 of one (checked only when the
     cache is non-empty, so an unpopulated cache never produces false "did you mean"s). The server
     cannot know this: it mints any novel slug, so a typo quietly becomes a near-duplicate topic in
     the permanent vocabulary, reported back only as an ordinary `organic_topics_minted`.

The rule fires at most once per (session, epoch) via its flag -- epoch bumps clear flags, per the
state contract, so a `clear` re-arms it. If it does not fire, the hook is silent and state is
untouched.

Fail-open, silent-always: any exception anywhere is swallowed by the top-level guard below and
the process always exits 0 (matches state.py's own fail-open contract). Bad stdin, a missing
session id, or a corrupt state file all degrade to silence rather than a crash or a stray nudge.

Run directly: python3 publish_lint.py < envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_publish_lint.py (drives it via publish-lint.sh, end to end).
"""

import json
import os
import sys

# publish_lint.py always runs as a script file (never `python3 -c ...`), so Python has already put
# its own directory at sys.path[0] -- `import state` below resolves state.py as a sibling module
# without any path manipulation (see state.py's own docstring).
import kb_tool
import state as state_mod

CONTEXT_PREFIX = "gitian-kb publish lint (advisory -- the call was not blocked):"

NEAR_MISS_FLAG = "lint_near_miss"
NEAR_MISS_MAX_DISTANCE = 2

# The tools whose `topics`/`mentions` this lint reads when they ride inside a batch_write -- the
# exact set the matcher above covers, by bare name (an operation names its tool without the
# `mcp__..._gitian__` namespace). Any other tool name in an operation contributes nothing.
LINTED_BATCH_TOOLS = (
    "publish_doc",
    "publish_memory",
    "publish_entry",
    "append_entry",
    "patch_doc",
    "patch_memory",
)
BATCH_WRITE_MARKER = "batch_write"


def _server_key():
    """"${GITIAN_KB_URL:-https://gitian.dev}/api/mcp/kb" -- mirrors the shell default-expansion every
    other hook uses to key servers, so the vocab cache this reads matches what harvest.py wrote."""
    base = os.environ.get("GITIAN_KB_URL")
    if not base:
        base = "https://gitian.dev"
    return base + "/api/mcp/kb"


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


def _levenshtein(a, b):
    """Iterative single-row edit distance. Stdlib-only; inputs are short topic slugs so the plain
    O(len(a)*len(b)) DP is more than fast enough -- no need for early-exit optimizations."""
    if a == b:
        return 0
    len_a, len_b = len(a), len(b)
    if len_a == 0:
        return len_b
    if len_b == 0:
        return len_a
    prev_row = list(range(len_b + 1))
    for i in range(1, len_a + 1):
        curr_row = [i] + [0] * len_b
        char_a = a[i - 1]
        for j in range(1, len_b + 1):
            cost = 0 if char_a == b[j - 1] else 1
            curr_row[j] = min(
                curr_row[j - 1] + 1,  # insertion
                prev_row[j] + 1,  # deletion
                prev_row[j - 1] + cost,  # substitution
            )
        prev_row = curr_row
    return prev_row[len_b]


def _as_slug_list(value):
    """tool_input.topics / tool_input.mentions are expected to be lists of non-empty strings;
    anything else (missing, wrong shape, non-string entries) contributes nothing."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v]


def _mentioned_slugs(tool_name, tool_input):
    """Every topic/mention slug the call carries: the call's own for a standalone write, or each
    covered operation's `args` for a batch_write, in operation order. A malformed batch (no list,
    an operation that is not a `{tool, args}` mapping, an unlisted tool) contributes only what its
    well-formed covered operations carry."""
    if BATCH_WRITE_MARKER not in tool_name:
        return _as_slug_list(tool_input.get("topics")) + _as_slug_list(tool_input.get("mentions"))
    operations = tool_input.get("operations")
    if not isinstance(operations, list):
        return []
    slugs = []
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("tool") not in LINTED_BATCH_TOOLS:
            continue
        args = operation.get("args")
        if isinstance(args, dict):
            slugs += _as_slug_list(args.get("topics")) + _as_slug_list(args.get("mentions"))
    return slugs


def _cached_topics(state, server_key):
    server = state.get("servers", {}).get(server_key)
    topics = server.get("topics") if isinstance(server, dict) else None
    if not isinstance(topics, list):
        return []
    return [t for t in topics if isinstance(t, dict) and isinstance(t.get("slug"), str)]


def _check_near_miss(mentioned_slugs, cached_topics):
    """See the module docstring. Returns the advice text, or None when nothing is a near miss."""
    if not cached_topics:
        return None
    cached_slugs = [t["slug"] for t in cached_topics]
    cached_slug_set = set(cached_slugs)

    offenders = []
    seen = set()
    for slug in mentioned_slugs:
        if slug in cached_slug_set or slug in seen:
            continue
        seen.add(slug)
        best_match, best_distance = None, None
        for cached_slug in cached_slugs:
            distance = _levenshtein(slug, cached_slug)
            if distance <= NEAR_MISS_MAX_DISTANCE and (best_distance is None or distance < best_distance):
                best_match, best_distance = cached_slug, distance
        if best_match is not None:
            offenders.append((slug, best_match))

    if not offenders:
        return None
    parts = ['"%s" -- did you mean "%s"?' % (slug, match) for slug, match in offenders]
    # Factual, not imperative: the hooks reference warns that text framed as an out-of-band
    # command can trip prompt-injection defenses. The call has already gone through, so the
    # remedy is a follow-up revision, never a re-send.
    return (
        "possible typo(s) against the cached vocabulary: "
        + "; ".join(parts)
        + ". If one was a typo, the item keeps it until a follow-up revision corrects its "
        "topics/mentions (`patch_doc`/`patch_memory` replace those lists wholesale), and a slug "
        "the server reports as `organic_topics_minted` stays in the vocabulary until "
        "`retract_topic` removes it."
    )


def _touch_session(state, sid):
    sessions = state.setdefault("sessions", {})
    session = sessions.get(sid)
    if not isinstance(session, dict):
        session = {}
        sessions[sid] = session
    state_mod.ensure_session_shape(session)
    return session


def lint(payload):
    """Guard for the payload: returns `decide`, a one-arg callable (decide(state)) that runs INSIDE
    the state lock against the freshly loaded state and returns the advice text, or None when
    nothing should fire (flag already consumed, or no near miss) -- in which case the caller
    (`_apply`) must not touch state at all. Returns None when the top-level guard itself fails
    (not a gitian call, or no session id) -- there is no decision to make at all."""
    tool_name = payload.get("tool_name")
    if not kb_tool.is_kb_tool(tool_name):
        return None
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None

    def decide(state):
        existing_session = state.get("sessions", {}).get(sid)
        existing_session = existing_session if isinstance(existing_session, dict) else {}
        flags = existing_session.get("flags")
        flags = flags if isinstance(flags, dict) else {}
        if flags.get(NEAR_MISS_FLAG):
            return None

        cached_topics = _cached_topics(state, _server_key())
        mentioned = _mentioned_slugs(tool_name, tool_input)
        return _check_near_miss(mentioned, cached_topics)

    return decide


def _apply(path, decide, sid):
    """One locked read-modify-write: decide against the freshly loaded state and, only if the rule
    fired, persist its flag and return the advice text. Returns None (no write) otherwise."""

    def mutate():
        state = state_mod.load(path)
        text = decide(state)
        if not text:
            return None

        session = _touch_session(state, sid)
        session["flags"][NEAR_MISS_FLAG] = True
        session["updatedAt"] = state_mod.now_iso()
        state_mod.save(path, state_mod.finalize(state))
        return text

    return state_mod.with_lock(path, mutate)


def main():
    payload = _parse_stdin()
    decide = lint(payload)
    if decide is None:
        return

    sid = payload.get("session_id")
    text = _apply(state_mod.state_path(), decide, sid)
    if not text:
        return

    # additionalContext and NOTHING else under hookSpecificOutput: no permissionDecision, so the
    # call is neither blocked nor pre-approved.
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": CONTEXT_PREFIX + "\n- " + text,
        }
    }
    sys.stdout.write(json.dumps(output))
    sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit
    sys.exit(0)
