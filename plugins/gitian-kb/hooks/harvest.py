#!/usr/bin/env python3
"""harvest.py -- PostToolUse harvester for the gitian-kb plugin's nudge layer.

Invoked as a single python3 process (stdin passed straight through, unread by harvest.sh) by
harvest.sh, itself registered as a PostToolUse hook matching "mcp__.*gitian.*|ReadMcpResourceTool".
Passively mines every gitian MCP call for state worth remembering across turns/sessions: the
server's vocab revision, a cached snapshot of its topic list, discipline counters (gitianReads,
publishes), and publish/append timestamps -- all folded into the shared state file (see state.py)
behind a single locked read-modify-write per invocation.

Fail-open, silent-always for the harvest pass proper: vocab/read/publish tracking never prints
anything. The one exception is the mint-description follow-up (see `mint_followup()` below): the
first time a session sees a given auto-minted (organic, undescribed) topic slug in a gitian
response's `organic_topics_minted` warning, it emits one PostToolUse additionalContext line
naming it; a slug already in this session's `mintPrompted` stays silent forever after. Any
exception anywhere is swallowed by the top-level guard below and the process always exits 0
(matches state.py's own fail-open contract).

Run directly: python3 harvest.py < envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_harvest.py (drives it via harvest.sh, end to end).
"""

import json
import os
import re
import sys

# harvest.py always runs as a script file (never `python3 -c ...`), so Python has already put
# its own directory at sys.path[0] -- `import state` below resolves state.py as a sibling module
# without any path manipulation (see state.py's own docstring: "sibling hook glue can `import
# state` directly rather than shelling out").
import state as state_mod

READ_SUFFIXES = ("get", "search", "list", "neighbors", "topic", "history", "file_intents")
PUBLISH_MARKERS = ("publish_doc", "publish_memory", "publish_entry", "publish_topic", "append_entry")
APPEND_MARKERS = ("append_entry", "publish_entry")
RETRACT_MARKERS = ("retract_item", "retract_topic")
MAX_TOPICS = 200

# Fallback regexes for the ORIGINAL, unnested/top-level shape (e.g. a test fixture or a future
# transport that puts these fields directly on the envelope/response, not inside an MCP content
# block). A REAL MCP tool response nests the server's JSON as an ESCAPED STRING inside
# {"content": [{"type": "text", "text": "{\"vocab_rev\": 19, ...}"}]} -- these patterns never
# match THAT text (the escaped `\"` breaks the literal `"vocab_rev"`/`"slug"` match), which is
# why _max_vocab_rev/_first_slug/_publish_succeeded below ALSO decode and structurally inspect
# each content block via _decoded_blocks. Kept here as the fallback for the unnested shape.
VOCAB_REV_RE = re.compile(r'"vocab_rev"\s*:\s*(\d+)')
SLUG_RE = re.compile(r'"slug"\s*:\s*"([^"]*)"')

# The mint-description follow-up (T8): the live server warning shape (linksWarnings() in
# src/lib/kb/mcp-server.ts, documented in docs/kb-mcp-transport.md) is a LintWarning object
# {"code": "organic_topics_minted", "path": "topics", "note": "auto-minted as organic, live
# immediately: <slug>[, <slug>...]"} -- slugs live in the prose `note`, not as a structured
# array. `_find_mint_warning` also tolerates a hypothetical/forward-compatible shape where
# "organic_topics_minted" is itself a JSON member holding the slugs directly (list of strings,
# list of {"slug": ...} objects, or a dict nesting either under a wrapper key) -- kept as a
# defensive fallback per the spec, even though it isn't what the server emits today.
MINT_WARNING_CODE = "organic_topics_minted"
MINT_SLUG_TOKEN_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")  # same kebab shape as topicSlugIssue


def _server_key():
    """"${GITIAN_KB_URL:-https://gitian.dev}/api/mcp" -- mirrors the shell default-expansion
    every other hook uses to key servers, so all hooks land on the same state key."""
    base = os.environ.get("GITIAN_KB_URL")
    if not base:
        base = "https://gitian.dev"
    return base + "/api/mcp"


def _parse_stdin():
    """Read the whole hook-input envelope. Unparsable/non-object stdin -> ({}, raw text)."""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    return raw, payload


def _is_gitian_call(tool_name, tool_input):
    """Guard: a gitian MCP tool call, or a ReadMcpResourceTool read of a gitian-kb:// resource."""
    if isinstance(tool_name, str) and "gitian" in tool_name:
        return True
    if tool_name == "ReadMcpResourceTool":
        uri = tool_input.get("uri")
        return isinstance(uri, str) and uri.startswith("gitian-kb://")
    return False


def _is_vocab_read(tool_name, tool_input):
    if tool_name != "ReadMcpResourceTool":
        return False
    uri = tool_input.get("uri")
    return isinstance(uri, str) and "gitian-kb://vocab" in uri


def _is_read_call(tool_name, is_vocab_read):
    if is_vocab_read:
        return True
    if not isinstance(tool_name, str):
        return False
    # publish_topic/retract_topic both end in the "topic" read-suffix -- a publish or retract call
    # takes precedence over the suffix match so it isn't double-counted as an orientation read.
    if _is_publish_call(tool_name) or _is_retract_call(tool_name):
        return False
    return any(tool_name.endswith(suffix) for suffix in READ_SUFFIXES)


def _is_publish_call(tool_name):
    return isinstance(tool_name, str) and any(marker in tool_name for marker in PUBLISH_MARKERS)


def _is_append_call(tool_name):
    return isinstance(tool_name, str) and any(marker in tool_name for marker in APPEND_MARKERS)


def _is_retract_call(tool_name):
    return isinstance(tool_name, str) and any(marker in tool_name for marker in RETRACT_MARKERS)


def _text_blocks(tool_response):
    """MCP tool responses are typically {"content"|"contents": [{"text": "<json>"}, ...]}."""
    blocks = []
    if isinstance(tool_response, dict):
        for key in ("content", "contents"):
            items = tool_response.get(key)
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        blocks.append(item["text"])
    return blocks


def _decoded_blocks(tool_response):
    """Yield each `_text_blocks` entry that successfully json.loads-decodes. A REAL MCP tool
    response nests the server's actual JSON payload as an ESCAPED STRING inside a content block
    -- {"content": [{"type": "text", "text": "{\\"vocab_rev\\": 19, ...}"}]} -- so a plain regex
    or substring check run over the JSON-rendered envelope never sees an unescaped '"vocab_rev"'
    or '"isError": true'; it sees '\\"vocab_rev\\"' instead, which doesn't match. Decoding each
    block and inspecting the resulting object structurally (see _max_vocab_rev/_first_slug/
    _publish_succeeded below) is what actually reaches the server's real fields. A block that
    fails to parse is skipped, never raised."""
    for text in _text_blocks(tool_response):
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        yield parsed


def _response_text(tool_response):
    """JSON-render just the tool_response payload -- publish-success/slug detection is scoped to
    the response only (never the whole stdin envelope), so a legitimate tool_input body that
    merely contains the words "validation_failed" or "isError" as prose doesn't get misread as a
    failure marker. Contrast with _max_vocab_rev, which the spec explicitly scopes to the RAW
    stdin text."""
    if isinstance(tool_response, str):
        return tool_response
    try:
        return json.dumps(tool_response)
    except Exception:
        return ""


def _max_vocab_rev(raw_text, tool_response):
    """max() over every vocab_rev found anywhere: the raw-stdin regex (the original, unnested/
    top-level shape -- kept as a fallback, scoped to the whole raw envelope per the original
    spec) PLUS a top-level "vocab_rev" int found inside any successfully-decoded nested text
    block (the real MCP wire shape -- see _decoded_blocks). None when nothing is found either
    way."""
    candidates = [int(m) for m in VOCAB_REV_RE.findall(raw_text)]
    for parsed in _decoded_blocks(tool_response):
        if isinstance(parsed, dict):
            rev = parsed.get("vocab_rev")
            if isinstance(rev, int) and not isinstance(rev, bool):
                candidates.append(rev)
    if not candidates:
        return None
    try:
        return max(candidates)
    except Exception:
        return None


def _first_slug(resp_text, tool_response):
    """The first slug found: the raw resp_text regex (the original, unnested/top-level shape --
    kept as a fallback, scoped to the response only per _response_text's own rationale) first,
    else a top-level "slug" string inside any successfully-decoded nested text block (the real
    MCP wire shape -- see _decoded_blocks)."""
    match = SLUG_RE.search(resp_text)
    if match:
        return match.group(1)
    for parsed in _decoded_blocks(tool_response):
        if isinstance(parsed, dict):
            slug = parsed.get("slug")
            if isinstance(slug, str) and slug:
                return slug
    return None


def _publish_succeeded(resp_text, tool_response):
    """A publish call is a success unless a failure marker is found -- checked both over the raw
    resp_text (the original, unnested/top-level shape: a literal '"isError": true' or a
    "validation_failed" substring) AND over each nested content block's own raw text (a
    "validation_failed" substring survives JSON-escaping unchanged, since escaping only touches
    quote/backslash/control characters, never the plain letters of that word) AND structurally
    over each successfully-decoded block (a decoded isError == True -- the one marker that a raw
    substring check on the escaped '\\"isError\\": true' text would miss; see _decoded_blocks)."""
    if '"isError": true' in resp_text or "validation_failed" in resp_text:
        return False
    for text in _text_blocks(tool_response):
        if "validation_failed" in text:
            return False
    for parsed in _decoded_blocks(tool_response):
        if isinstance(parsed, dict) and parsed.get("isError") is True:
            return False
    return True


def _topics_from(obj):
    if isinstance(obj, dict):
        topics = obj.get("topics")
        if isinstance(topics, list):
            return topics
    return None


def _extract_topics(tool_response):
    """Try json.loads on each content block's text field; fall back to the whole response;
    None on total failure (harvest nothing)."""
    for text in _text_blocks(tool_response):
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        topics = _topics_from(parsed)
        if topics is not None:
            return topics
    try:
        parsed = tool_response if isinstance(tool_response, dict) else json.loads(tool_response)
    except Exception:
        return None
    return _topics_from(parsed)


def _normalize_topics(raw_topics):
    normalized = []
    for entry in raw_topics:
        if not isinstance(entry, dict):
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        description = entry.get("description")
        if not isinstance(description, str):
            description = ""
        degree = entry.get("degree")
        if not isinstance(degree, (int, float)) or isinstance(degree, bool):
            degree = 0
        normalized.append({"slug": slug, "description": description, "degree": degree})
    return normalized[:MAX_TOPICS]


def harvest(raw_text, payload):
    """Pure computation over the parsed envelope -> an effect dict describing what to write, or
    None if the guard fails or there is nothing worth harvesting. Never touches the state file."""
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    tool_response = payload.get("tool_response")
    sid = payload.get("session_id")

    if not _is_gitian_call(tool_name, tool_input):
        return None
    if not isinstance(sid, str) or not sid:
        return None

    is_vocab_read = _is_vocab_read(tool_name, tool_input)
    server_updates = {}
    session_vocab_rev = None

    vocab_rev = _max_vocab_rev(raw_text, tool_response)
    if vocab_rev is not None:
        server_updates["vocabRev"] = vocab_rev
        session_vocab_rev = vocab_rev

    if is_vocab_read:
        topics = _extract_topics(tool_response)
        if topics is not None:
            normalized = _normalize_topics(topics)
            server_updates["topics"] = normalized
            server_updates["vocabFetchedAt"] = state_mod.now_iso()
            server_updates["undescribedTopics"] = [
                t["slug"] for t in normalized if not t["description"]
            ]

    incr_reads = _is_read_call(tool_name, is_vocab_read)

    incr_publishes = False
    resp_text = _response_text(tool_response)
    if _is_publish_call(tool_name) and _publish_succeeded(resp_text, tool_response):
        incr_publishes = True
        server_updates["lastPublishAt"] = state_mod.now_iso()
        slug = _first_slug(resp_text, tool_response)
        if slug is not None:
            server_updates["lastPublishSlug"] = slug
        if _is_append_call(tool_name):
            server_updates["lastAppendAt"] = state_mod.now_iso()

    if not server_updates and not incr_reads and not incr_publishes:
        return None

    return {
        "server_key": _server_key(),
        "server_updates": server_updates,
        "session_id": sid,
        "session_vocab_rev": session_vocab_rev,
        "incr_reads": incr_reads,
        "incr_publishes": incr_publishes,
    }


def _as_counter(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return value


def _touch_server(state, key):
    servers = state.setdefault("servers", {})
    server = servers.get(key)
    if not isinstance(server, dict):
        server = {}
        servers[key] = server
    return server


def _touch_session(state, sid):
    sessions = state.setdefault("sessions", {})
    session = sessions.get(sid)
    if not isinstance(session, dict):
        session = {}
        sessions[sid] = session
    state_mod.ensure_session_shape(session)
    return session


def apply_effect(effect):
    """One locked read-modify-write applying every field the effect describes."""
    if effect is None:
        return
    path = state_mod.state_path()

    def mutate():
        state = state_mod.load(path)

        server_updates = effect["server_updates"]
        if server_updates:
            server = _touch_server(state, effect["server_key"])
            if "vocabRev" in server_updates:
                server["vocabRev"] = max(server_updates["vocabRev"], _as_counter(server.get("vocabRev")))
            for field in ("topics", "vocabFetchedAt", "undescribedTopics", "lastPublishAt",
                          "lastPublishSlug", "lastAppendAt"):
                if field in server_updates:
                    server[field] = server_updates[field]

        if effect["session_vocab_rev"] is not None or effect["incr_reads"] or effect["incr_publishes"]:
            session = _touch_session(state, effect["session_id"])
            if effect["session_vocab_rev"] is not None:
                session["lastSeenVocabRev"] = max(
                    effect["session_vocab_rev"], _as_counter(session.get("lastSeenVocabRev"))
                )
            if effect["incr_reads"]:
                session["gitianReads"] = _as_counter(session.get("gitianReads")) + 1
            if effect["incr_publishes"]:
                session["publishes"] = _as_counter(session.get("publishes")) + 1
            session["updatedAt"] = state_mod.now_iso()

        state_mod.save(path, state_mod.finalize(state))

    state_mod.with_lock(path, mutate)


def _find_mint_warning(node):
    """Recursively search a parsed JSON value for the organic_topics_minted warning, wherever it
    nests. Returns a dict describing what was found (see below), or None if it isn't anywhere in
    `node`. Two shapes are recognized:
      - {"kind": "warning", "note": <str>} -- a LintWarning-shaped object with
        code == "organic_topics_minted" and a string `note` (the real server shape: slugs are
        prose inside the note, e.g. "...live immediately: a, b").
      - {"kind": "member", "value": <any>} -- a dict literally keyed "organic_topics_minted"
        (defensive fallback for a hypothetical structured shape; not what the server emits today).
    Also descends into string values that themselves parse as JSON, since MCP tool responses
    nest a JSON-encoded string inside {"content": [{"type": "text", "text": "<json>"}]}."""
    if isinstance(node, dict):
        if node.get("code") == MINT_WARNING_CODE and isinstance(node.get("note"), str):
            return {"kind": "warning", "note": node["note"]}
        if MINT_WARNING_CODE in node:
            return {"kind": "member", "value": node[MINT_WARNING_CODE]}
        for value in node.values():
            found = _find_mint_warning(value)
            if found is not None:
                return found
        return None
    if isinstance(node, list):
        for item in node:
            found = _find_mint_warning(item)
            if found is not None:
                return found
        return None
    if isinstance(node, str):
        try:
            parsed = json.loads(node)
        except Exception:
            return None
        if isinstance(parsed, (dict, list)):
            return _find_mint_warning(parsed)
        return None
    return None


def _slugs_from_note(note):
    """The real warning's note is prose ending in "...: slug1, slug2" (see linksWarnings() in
    src/lib/kb/mcp-server.ts) -- take the text after the last colon, split on commas, and keep
    only tokens that look like a real kebab-case topic slug (same shape as topicSlugIssue in
    src/lib/kb/topics.ts, inlined here rather than imported since that module is read-only and
    hook scripts don't import app TypeScript anyway). A note that doesn't match this shape at all
    yields no tokens -- never a crash, never a garbage slug."""
    tail = note.rsplit(":", 1)[-1]
    tokens = [tok.strip() for tok in tail.split(",")]
    return [tok for tok in tokens if MINT_SLUG_TOKEN_RE.match(tok)]


def _slugs_from_member_value(node):
    """Fallback extractor for the defensive "organic_topics_minted is itself a JSON member"
    shape: a plain list of slug strings, a list of {"slug": ...} objects, or a dict nesting
    either under a wrapper key ("slugs"/"topics"/"items"/"minted") or a direct "slug" scalar.
    Bare strings are only ever read as slugs when they appear as list elements -- never as an
    arbitrary dict value -- so an unrelated prose field can't be misread as a topic slug."""
    slugs = []
    if isinstance(node, list):
        for item in node:
            if isinstance(item, str):
                if item:
                    slugs.append(item)
            elif isinstance(item, dict):
                slug = item.get("slug")
                if isinstance(slug, str) and slug:
                    slugs.append(slug)
                else:
                    slugs.extend(_slugs_from_member_value(item))
    elif isinstance(node, dict):
        slug = node.get("slug")
        if isinstance(slug, str) and slug:
            slugs.append(slug)
        for key in ("slugs", "topics", "items", "minted"):
            if key in node:
                slugs.extend(_slugs_from_member_value(node[key]))
    return slugs


def _extract_minted_slugs(raw_text, tool_response):
    """Defensive end-to-end extraction: a cheap substring gate on the raw stdin text (matches the
    spec: "when the raw envelope text contains organic_topics_minted"), then a structured search
    scoped to tool_response only -- never tool_input, mirroring _response_text's scoping
    rationale so prose in a request body can't forge a mint warning. Any parse failure anywhere,
    or a shape that doesn't resolve to anything, yields an empty list -- never an exception."""
    if MINT_WARNING_CODE not in raw_text:
        return []
    try:
        found = _find_mint_warning(tool_response)
        if found is None:
            return []
        if found["kind"] == "warning":
            slugs = _slugs_from_note(found["note"])
        else:
            slugs = _slugs_from_member_value(found["value"])
    except Exception:
        return []
    seen = set()
    ordered = []
    for slug in slugs:
        if slug not in seen:
            seen.add(slug)
            ordered.append(slug)
    return ordered


def _mint_message(slugs):
    plural = len(slugs) != 1
    return (
        "%s %s %s auto-minted without descriptions -- worth an immediate publish_topic for each "
        "with a real one-line description so the vocabulary stays legible; advisory, once per "
        "topic per session."
        % ("topics" if plural else "topic", ", ".join(slugs), "were" if plural else "was")
    )


def _apply_mint_followup(server_key, sid, slugs):
    """One locked read-modify-write: for every slug not already in this session's mintPrompted,
    add it there and to the server's undescribedTopics. Returns just the newly-prompted slugs
    (order preserved) so the caller can build a message naming only what's new -- an empty list
    when every extracted slug was already prompted this session, meaning stay silent."""
    path = state_mod.state_path()
    fresh_slugs = []

    def mutate():
        state = state_mod.load(path)
        session = _touch_session(state, sid)
        prompted = session.get("mintPrompted")
        prompted = prompted if isinstance(prompted, list) else []
        prompted_set = set(s for s in prompted if isinstance(s, str))
        fresh = [s for s in slugs if s not in prompted_set]
        if not fresh:
            return

        session["mintPrompted"] = prompted + fresh
        session["updatedAt"] = state_mod.now_iso()

        server = _touch_server(state, server_key)
        undescribed = server.get("undescribedTopics")
        undescribed = list(undescribed) if isinstance(undescribed, list) else []
        undescribed_set = set(s for s in undescribed if isinstance(s, str))
        for slug in fresh:
            if slug not in undescribed_set:
                undescribed.append(slug)
                undescribed_set.add(slug)
        server["undescribedTopics"] = undescribed

        state_mod.save(path, state_mod.finalize(state))
        fresh_slugs.extend(fresh)

    state_mod.with_lock(path, mutate)
    return fresh_slugs


def mint_followup(raw_text, payload):
    """Extension to the harvest pass: on the SAME invocation as harvest()/apply_effect() above,
    inspect the envelope for an organic_topics_minted warning and, for any slug this session
    hasn't been prompted about yet, record it and return one PostToolUse additionalContext string
    naming every new slug. Returns None when there is nothing new to prompt -- including: not a
    gitian call, no session id, no warning present, a parse failure, or every extracted slug
    already being in mintPrompted (silent, per spec)."""
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    sid = payload.get("session_id")

    if not _is_gitian_call(tool_name, tool_input):
        return None
    if not isinstance(sid, str) or not sid:
        return None

    slugs = _extract_minted_slugs(raw_text, payload.get("tool_response"))
    if not slugs:
        return None

    fresh = _apply_mint_followup(_server_key(), sid, slugs)
    if not fresh:
        return None
    return _mint_message(fresh)


def main():
    raw_text, payload = _parse_stdin()
    effect = harvest(raw_text, payload)
    apply_effect(effect)

    context = mint_followup(raw_text, payload)
    if context:
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": context,
                    }
                }
            )
        )
        sys.stdout.write("\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit
    sys.exit(0)
