#!/usr/bin/env python3
"""publish_lint.py -- PreToolUse publish lint for the gitian-kb plugin's nudge layer.

Invoked as a single python3 process (stdin passed straight through, unread by publish-lint.sh) by
publish-lint.sh, itself registered as a PreToolUse hook matching
"mcp__.*(publish_doc|publish_memory|publish_entry|append_entry)". Advisory only: it never blocks
a call outright -- an identical re-send always passes untouched (pinned decision 5) -- it just
nudges toward better vocabulary hygiene before a topic/mention list is committed to the KB.

Guard: tool_name must contain "gitian" (matches the harvest.py convention); anything else is
silent. The matcher above is only a coarse pre-filter -- this guard is the real gate.

Retry short-circuit: every call is hashed (sha256 of tool_name + canonical JSON of tool_input).
If that hash is already in sessions.<sid>.lintHashes, the call is treated as an identical retry
of a previously-linted call and passes silently, untouched -- this is what lets the model "just
re-send the identical call" after reading an advisory nudge.

Rules (each evaluated independently; each fires at most once per (session, epoch) via its own
flag -- epoch bumps clear flags, per the state contract, so a `clear` re-arms every rule):
  r1 empty-topics (flag lint_empty_topics) -- tool is publish_doc, publish_memory, or
     publish_entry (append_entry is EXEMPT, per pinned decision 5: entries are only linted on
     create) and tool_input.topics is missing or empty. Reason lists up to 8 cached-vocab
     candidates ranked by degree as "slug - description"; when the cache is empty, advises
     reading gitian-kb://vocab and linking 1-3 topics instead.
  r2 near-miss (flag lint_near_miss) -- any slug in tool_input.topics + tool_input.mentions that
     is NOT an exact cached slug but sits within Levenshtein distance 2 of one (checked only when
     the cache is non-empty, so an unpopulated cache never produces false "did you mean"s). Guards
     a typo from quietly minting a near-duplicate into the permanent vocabulary.
  r3 project-name (flag lint_project_topic) -- any slug in topics+mentions equal, case-insensitive,
     to tool_input.project or to the basename of tool_input.repo. A project-name topic adds no
     relatedness signal (every doc in the repo would carry it) -- suggests a concept topic instead.

If no rule fires (or every rule whose condition matches already had its flag consumed this
epoch), the hook is silent and the call hash is deliberately NOT stored -- there is nothing to
remember a retry of. If at least one rule fires, every triggered flag plus the call hash are
written in a single state merge, and one PreToolUse deny-once JSON is emitted whose reason starts
with the fixed advisory preamble followed by one bullet per triggered rule.

Fail-open, silent-always: any exception anywhere is swallowed by the top-level guard below and
the process always exits 0 (matches state.py's own fail-open contract). Bad stdin, a missing
session id, or a corrupt state file all degrade to silence rather than a crash or a stray nudge.

Run directly: python3 publish_lint.py < envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_publish_lint.py (drives it via publish-lint.sh, end to end).
"""

import hashlib
import json
import os
import sys

# publish_lint.py always runs as a script file (never `python3 -c ...`), so Python has already put
# its own directory at sys.path[0] -- `import state` below resolves state.py as a sibling module
# without any path manipulation (see state.py's own docstring).
import state as state_mod

REASON_PREFIX = "gitian-kb publish lint (advisory - re-send the identical call to proceed unchanged):"

# r1 applies to these create-shaped calls only; append_entry is exempt (pinned decision 5).
EMPTY_TOPICS_MARKERS = ("publish_doc", "publish_memory", "publish_entry")
APPEND_MARKER = "append_entry"

MAX_CANDIDATES = 8
NEAR_MISS_MAX_DISTANCE = 2


def _server_key():
    """"${GITIAN_KB_URL:-https://gitian.dev}/api/mcp" -- mirrors the shell default-expansion every
    other hook uses to key servers, so the vocab cache this reads matches what harvest.py wrote."""
    base = os.environ.get("GITIAN_KB_URL")
    if not base:
        base = "https://gitian.dev"
    return base + "/api/mcp"


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


def _call_hash(tool_name, tool_input):
    canonical = tool_name + json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _basename(path):
    if not isinstance(path, str) or not path:
        return ""
    trimmed = path.rstrip("/")
    if not trimmed:
        return ""
    return trimmed.rsplit("/", 1)[-1]


def _degree(topic):
    value = topic.get("degree")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _cached_topics(state, server_key):
    server = state.get("servers", {}).get(server_key)
    topics = server.get("topics") if isinstance(server, dict) else None
    if not isinstance(topics, list):
        return []
    return [t for t in topics if isinstance(t, dict) and isinstance(t.get("slug"), str)]


def _check_empty_topics(tool_name, tool_input, cached_topics):
    """r1 -- see module docstring."""
    if not any(marker in tool_name for marker in EMPTY_TOPICS_MARKERS):
        return None
    if APPEND_MARKER in tool_name:
        return None  # append_entry exempt from r1 regardless of any other marker match
    provided = tool_input.get("topics")
    if isinstance(provided, list) and len(provided) > 0:
        return None

    if not cached_topics:
        return (
            "no topics linked, and the cached vocabulary is empty -- read `gitian-kb://vocab` "
            "and link 1-3 topics before publishing"
        )

    ranked = sorted(cached_topics, key=_degree, reverse=True)[:MAX_CANDIDATES]
    candidates = ["%s - %s" % (t["slug"], t.get("description") or "") for t in ranked]
    return "no topics linked -- top cached-vocab candidates by relatedness: " + "; ".join(candidates)


def _check_near_miss(mentioned_slugs, cached_topics):
    """r2 -- see module docstring."""
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
    return "possible typo(s) against the cached vocabulary: " + "; ".join(parts)


def _check_project_topic(tool_input, mentioned_slugs):
    """r3 -- see module docstring."""
    identity_names = set()
    project = tool_input.get("project")
    if isinstance(project, str) and project:
        identity_names.add(project.lower())
    repo_base = _basename(tool_input.get("repo"))
    if repo_base:
        identity_names.add(repo_base.lower())
    if not identity_names:
        return None

    offenders = []
    seen = set()
    for slug in mentioned_slugs:
        if slug in seen:
            continue
        if slug.lower() in identity_names:
            seen.add(slug)
            offenders.append(slug)

    if not offenders:
        return None
    quoted = ", ".join('"%s"' % slug for slug in offenders)
    return (
        "%s look%s like the project/repo name, not a concept -- a project-name topic adds no "
        "relatedness signal (every doc in the repo would carry it); consider a concept topic instead"
        % (quoted, "s" if len(offenders) == 1 else "")
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
    """Guard + call-hash computation for the payload: returns (decide, call_hash), where `decide`
    is a one-arg callable (decide(state)) that runs INSIDE the state lock against the freshly
    loaded state and returns the list of (flag_name, text) pairs that trigger, or None when
    nothing should fire (identical retry, or no rule matched) -- in which case the caller
    (`_apply`) must not touch state at all. Returns (None, None) when the top-level guard itself
    fails (not a gitian call, or no session id) -- there is no decision to make at all."""
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or "gitian" not in tool_name:
        return None, None
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None, None

    call_hash = _call_hash(tool_name, tool_input)

    def decide(state):
        existing_session = state.get("sessions", {}).get(sid)
        existing_session = existing_session if isinstance(existing_session, dict) else {}
        lint_hashes = existing_session.get("lintHashes")
        lint_hashes = lint_hashes if isinstance(lint_hashes, list) else []
        if call_hash in lint_hashes:
            return None  # identical retry -- silent, state untouched (pinned decision 5)

        flags = existing_session.get("flags")
        flags = flags if isinstance(flags, dict) else {}

        cached_topics = _cached_topics(state, _server_key())
        mentioned = _as_slug_list(tool_input.get("topics")) + _as_slug_list(tool_input.get("mentions"))

        triggered = []
        if not flags.get("lint_empty_topics"):
            text = _check_empty_topics(tool_name, tool_input, cached_topics)
            if text is not None:
                triggered.append(("lint_empty_topics", text))
        if not flags.get("lint_near_miss"):
            text = _check_near_miss(mentioned, cached_topics)
            if text is not None:
                triggered.append(("lint_near_miss", text))
        if not flags.get("lint_project_topic"):
            text = _check_project_topic(tool_input, mentioned)
            if text is not None:
                triggered.append(("lint_project_topic", text))

        return triggered or None

    return decide, call_hash


def _apply(path, decide, sid, call_hash):
    """One locked read-modify-write: decide against the freshly loaded state and, only if
    something triggered, persist the flags + hash and return the triggered bullet texts. Returns
    None (no write) when the retry short-circuit hits or nothing triggers."""

    def mutate():
        state = state_mod.load(path)
        triggered = decide(state)
        if not triggered:
            return None  # no rule identified anything -- do NOT store the hash either

        session = _touch_session(state, sid)
        for flag_name, _text in triggered:
            session["flags"][flag_name] = True
        lint_hashes = session.get("lintHashes")
        if not isinstance(lint_hashes, list):
            lint_hashes = []
        lint_hashes.append(call_hash)
        session["lintHashes"] = lint_hashes
        session["updatedAt"] = state_mod.now_iso()
        state_mod.save(path, state_mod.finalize(state))
        return [text for _flag_name, text in triggered]

    return state_mod.with_lock(path, mutate)


def main():
    payload = _parse_stdin()
    decide, call_hash = lint(payload)
    if decide is None:
        return

    sid = payload.get("session_id")
    path = state_mod.state_path()
    triggered_texts = _apply(path, decide, sid, call_hash)
    if not triggered_texts:
        return

    reason = REASON_PREFIX + "\n" + "\n".join("- %s" % text for text in triggered_texts)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
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
