#!/usr/bin/env python3
"""harvest.py -- PostToolUse harvester for the gitian-kb plugin's nudge layer.

Invoked as a single python3 process (stdin passed straight through, unread by harvest.sh) by
harvest.sh, itself registered as a PostToolUse hook matching the KB server's tool namespace
(`mcp__(plugin_gitian-kb_)?[gG][iI][tT][iI][aA][nN]__.*`, see kb_tool.py) or ReadMcpResourceTool --
never the code server's tools, whose names a looser pattern would also catch.
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
import kb_tool
import plugin_update
import state as state_mod

# Multi-KB phase 1 (see [[multi-kb-core-plan]]): sessions.<sid>.lastSeenVocabRev is keyed per
# (server, kb) now (state.py's _SESSION_DEFAULTS), not a bare scalar -- harvest.py doesn't parse
# which kb a response targeted, so every observation here lands in the "home" bucket, same
# fallback session_digest.py's own reader/writer use.
DEFAULT_KB_SLUG = "home"

# Tools whose NAME alone makes a call an orientation read. `read_resource` is deliberately ABSENT
# ([[kb-scribe-delegation]]): it is the server's own resource-read tool -- the only channel a
# plugin SUBAGENT has, since it carries no ReadMcpResourceTool in its registry -- but four of its
# five uris are the STATIC `gitian-kb://format/*` publish-format instructions, which expose no KB
# content whatsoever. Crediting those would let any scribe dispatch satisfy the parent's
# orientation check ("has anything in this session looked at the KB yet") with no sweep having
# happened. Only `gitian-kb://vocab` is real KB content, and `_is_vocab_read` credits it below --
# matching the other spelling of the same read, `ReadMcpResourceTool`, which has never counted for
# a format uri either.
READ_SUFFIXES = (
    "get",
    "search",
    "list",
    "neighbors",
    "topic",
    "history",
    "changes",
    "file_intents",
)
# patch_doc/patch_memory ARE writes to the KB -- they carry no body, but they revise an item and
# are the scribe's normal revision path, so a session whose whole KB contribution was a patch must
# not read as "nothing published" to the Stop reminder (whose own marker list mirrors this one).
# batch_write is a write too: it carries up to 25 of the above in one call, so a scribe that
# imported a corpus through it has published exactly as much as one that made 25 calls. Its
# envelope has no top-level `slug`, which is why _publish_outcome reads a batch by its results.
PUBLISH_MARKERS = (
    "publish_doc",
    "publish_memory",
    "publish_entry",
    "publish_topic",
    "publish_category",
    "append_entry",
    "patch_doc",
    "patch_memory",
    "batch_write",
)
APPEND_MARKERS = ("append_entry", "publish_entry")
RETRACT_MARKERS = ("retract_item", "retract_topic")
MAX_TOPICS = 200

# Fallback regex for the ORIGINAL, unnested/top-level shape (e.g. a test fixture or a future
# transport that puts this field directly on the envelope/response, not inside an MCP content
# block). A REAL MCP tool response nests the server's JSON as an ESCAPED STRING inside
# {"content": [{"type": "text", "text": "{\"vocab_rev\": 19, ...}"}]} -- this pattern never
# matches THAT text (the escaped `\"` breaks the literal `"vocab_rev"` match), which is why
# _max_vocab_rev below ALSO decodes and structurally inspects each content block via
# _decoded_blocks. Kept here as the fallback for the unnested shape.
VOCAB_REV_RE = re.compile(r'"vocab_rev"\s*:\s*(\d+)')

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
    """"${GITIAN_KB_URL:-https://gitian.dev}/api/mcp/kb" -- mirrors the shell default-expansion
    every other hook uses to key servers, so all hooks land on the same state key. It is the
    plugin's own `.mcp.json` url (the canonical KB endpoint; bare `/api/mcp` is a deprecated alias)."""
    base = os.environ.get("GITIAN_KB_URL")
    if not base:
        base = "https://gitian.dev"
    return base + "/api/mcp/kb"


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
    """Guard: a call to a tool of the gitian KB server under either spelling (`read_resource`
    included) -- never the code server's tools, which also carry "gitian" in their names -- or a
    ReadMcpResourceTool read of a gitian-kb:// resource."""
    if kb_tool.is_kb_tool(tool_name):
        return True
    if tool_name == "ReadMcpResourceTool":
        uri = tool_input.get("uri")
        return isinstance(uri, str) and uri.startswith("gitian-kb://")
    return False


def _is_vocab_read(tool_name, tool_input):
    """Either spelling of the same read: a ReadMcpResourceTool read of gitian-kb://vocab (what a
    primary session does) or the server's `read_resource` tool called with that uri (what a
    subagent must do -- it has no resource-read tool). Both carry the uri in `tool_input.uri`.
    Their RESPONSES differ, though, and _extract_topics handles both: ReadMcpResourceTool answers
    `contents: [{text: "<vocab json>"}]`, while the tool answers `content: [{text: "<json of
    {uri, mimeType, text}>"}]` -- the vocabulary one level deeper, inside `text`.

    This is also the ONLY `read_resource` uri that earns an orientation read (see READ_SUFFIXES):
    the other four are the static format docs."""
    if not isinstance(tool_name, str):
        return False
    if tool_name != "ReadMcpResourceTool" and not tool_name.endswith("read_resource"):
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
    """Every text block in a tool response, across the three shapes it arrives in.

    The one Claude Code ACTUALLY hands a PostToolUse hook for an MCP tool call is a BARE LIST of
    content blocks -- [{"type": "text", "text": "<json>"}] -- captured from a live hook payload
    (Claude Code 2.1.272). Until 0.22.0 only the dict shapes below were read, so on real tool
    traffic this returned nothing: `vocab_rev` was never harvested, no write was ever counted (the
    Stop reminder then claimed "nothing published" in sessions that had published), and no mint
    follow-up ever fired -- while every test passed, because every fixture used the dict shape.
    The dict shapes stay: {"content": [...]} is the MCP wire envelope, and {"contents": [...]} is
    what a resource read (ReadMcpResourceTool) answers with."""
    blocks = []

    def collect(items):
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    blocks.append(item["text"])

    if isinstance(tool_response, list):
        collect(tool_response)
    elif isinstance(tool_response, dict):
        for key in ("content", "contents"):
            collect(tool_response.get(key))
    return blocks


def _decoded_blocks(tool_response):
    """Yield each `_text_blocks` entry that successfully json.loads-decodes. A REAL MCP tool
    response nests the server's actual JSON payload as an ESCAPED STRING inside a content block
    -- {"content": [{"type": "text", "text": "{\\"vocab_rev\\": 19, ...}"}]} -- so a plain regex
    or substring check run over the JSON-rendered envelope never sees an unescaped '"vocab_rev"'
    or '"isError": true'; it sees '\\"vocab_rev\\"' instead, which doesn't match. Decoding each
    block and inspecting the resulting object structurally (see _max_vocab_rev and
    _envelope_candidates below) is what actually reaches the server's real fields. A block that
    fails to parse is skipped, never raised."""
    for text in _text_blocks(tool_response):
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        yield parsed


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


def _envelope_candidates(tool_response):
    """Every dict that could be the server's own response envelope, best evidence first: each
    successfully-decoded nested content block (the REAL MCP wire shape) and then, as the
    documented fallback for the unnested/top-level shape, the tool_response itself. Scoped to the
    response -- never tool_input -- so a request body that merely quotes an error code as prose
    can neither forge a failure nor forge a success."""
    candidates = [p for p in _decoded_blocks(tool_response) if isinstance(p, dict)]
    if isinstance(tool_response, dict):
        candidates.append(tool_response)
    return candidates


def _is_failure_envelope(payload):
    """Every refusal the server can answer a write with is `errResult({error: "<code>", ...})`
    (mcp-server.ts) -- a STRING `error`, which is the ONE field they all share: `rev_conflict`,
    `base_rev_required`, `edit_no_match`, `patch_conflict`, `validation_failed`, `not_found`,
    `slug_taken`, `forbidden`, `internal`. `ok: false` is the repository layer's own internal
    spelling and `isError` the MCP transport's, both accepted so a shape that surfaces either one
    instead reads as the failure it is."""
    if isinstance(payload.get("error"), str) and payload["error"].strip():
        return True
    if payload.get("ok") is False:
        return True
    return payload.get("isError") is True


def _written_slug(payload):
    """The slug a success envelope names, or None. Normally the top-level `slug`; a batched
    `publish_topic` answers `{topics: [{slug, state, degree}, ...], landed_in}` instead -- no
    top-level slug, but each entry names what was written, so the first one is the proof."""
    slug = payload.get("slug")
    if isinstance(slug, str) and slug:
        return slug
    topics = payload.get("topics")
    if isinstance(topics, list):
        for entry in topics:
            if isinstance(entry, dict) and isinstance(entry.get("slug"), str) and entry["slug"]:
                return entry["slug"]
    return None


def _is_success_envelope(payload):
    """POSITIVE evidence that a write landed. Every successful write answers with the item's
    `slug` (repository.ts::PublishSuccess, and publish_topic's own `{slug, state, degree, landed_in}`), and
    the publish/patch/append tails add a numeric `rev`. `rev` is therefore required to be numeric
    only WHEN PRESENT -- publish_topic mints a topic with no revision number at all -- while the
    slug is unconditional: with no slug there is nothing to say was written, and the whole point
    of reading success positively is that "no failure marker found" is not evidence of one."""
    if _written_slug(payload) is None:
        return False
    rev = payload.get("rev")
    if rev is not None and (isinstance(rev, bool) or not isinstance(rev, (int, float))):
        return False
    return True


def _is_batch_envelope(payload):
    """`batch_write`'s answer: `{results: [...], succeeded: N, failed: M}` (mcp-server.ts). The two
    counters are required alongside the list so an ordinary payload that merely has a `results` key
    -- a `search` response, say -- is never read as one."""
    return (
        isinstance(payload.get("results"), list)
        and _is_int(payload.get("succeeded"))
        and _is_int(payload.get("failed"))
    )


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _landed_batch_entries(payload):
    """The batch's `results` entries that really changed the KB, in operation order: `ok: true`,
    carrying an item slug (the same positive evidence a standalone write is judged on), and not
    the `unchanged: true` no-op a write whose content already matched the head reports -- which,
    standalone or batched, records nothing."""
    return [
        entry
        for entry in payload["results"]
        if isinstance(entry, dict)
        and entry.get("ok") is True
        and _is_success_envelope(entry)
        and entry.get("unchanged") is not True
    ]


def _batch_landed_an_append(tool_name, tool_input, tool_response):
    """True when `tool_name` is a batch_write and one of the operations that landed was a journal
    write (`append_entry`/`publish_entry`). The commit-nudge damper keys on `lastAppendAt`, and a
    journal entry written through a batch is exactly the entry it is asking about. The response
    does not name each result's tool, so the operation is read back off `tool_input` by `index`."""
    if "batch_write" not in tool_name:
        return False
    operations = tool_input.get("operations")
    if not isinstance(operations, list):
        return False
    for payload in _envelope_candidates(tool_response):
        if not _is_batch_envelope(payload):
            continue
        for entry in _landed_batch_entries(payload):
            index = entry.get("index")
            if not _is_int(index) or not 0 <= index < len(operations):
                continue
            operation = operations[index]
            if isinstance(operation, dict) and operation.get("tool") in APPEND_MARKERS:
                return True
    return False


def _publish_outcome(tool_response):
    """The success envelope a write actually landed, or None when it recorded nothing. A failure
    anywhere in the response wins outright: an envelope that is BOTH (a decoded error block plus
    a slug-bearing sibling) is a failure, since the failure is the specific claim.

    A no-op does NOT count. The server reports a write whose content already matched the head as
    an ordinary success envelope carrying `unchanged: true` (repository.ts's `noop` plan) --
    nothing was stored, so the session contributed nothing to the KB, and the counter this feeds
    is what silences the Stop reminder for the rest of the epoch. Counting it would also hand an
    agent a way to buy that silence by re-sending an old body verbatim. The envelope's `vocab_rev`
    is still harvested; only the publish counter and the publish timestamps abstain."""
    success = None
    for payload in _envelope_candidates(tool_response):
        if _is_failure_envelope(payload):
            return None
        if success is not None:
            continue
        if _is_batch_envelope(payload):
            # A batch is a SUCCESS as a call even when some operations failed -- the failures are
            # inside `results`, never on the envelope -- so it counts as a publish exactly when at
            # least one operation landed a change, and names the last such operation's slug.
            landed = _landed_batch_entries(payload)
            success = landed[-1] if landed else None
        elif _is_success_envelope(payload):
            success = payload
    if success is None:
        return None
    if success.get("unchanged") is True:
        return None
    return success


def _topics_from(obj):
    if isinstance(obj, dict):
        topics = obj.get("topics")
        if isinstance(topics, list):
            return topics
    return None


def _nested_resource_payload(parsed):
    """The resource document inside a `read_resource` TOOL response. That tool answers
    `jsonResult({uri, mimeType, text})` (mcp-server.ts), so the vocabulary is JSON-encoded TWICE:
    once as the resource's own `text`, and again as the content block's `text`. A reader that
    decodes one level lands on `{"uri": ..., "mimeType": ..., "text": "{\\"topics\\": [...]}"}`
    and finds no `topics` key at all -- which is why the vocab cache silently stopped filling the
    moment subagents started reading the vocabulary through the tool instead of through
    `ReadMcpResourceTool` (whose `contents[].text` IS the document, one level shallower).
    None when this block isn't that shape, or its inner text isn't JSON."""
    if not isinstance(parsed, dict):
        return None
    inner = parsed.get("text")
    if not isinstance(inner, str):
        return None
    try:
        return json.loads(inner)
    except Exception:
        return None


def _vocab_payload(tool_response):
    """The vocabulary document itself -- the dict carrying a `topics` list -- inside a vocab read's
    response: try json.loads on each content block's text field, then, for the `read_resource`
    tool's doubly-encoded envelope, on the resource payload nested inside it; fall back to the
    whole response; None on total failure (harvest nothing). `topics` and `default_kb` are both
    top-level members of this one document, so both are read off the same parse."""
    for text in _text_blocks(tool_response):
        try:
            parsed = json.loads(text)
        except Exception:
            continue
        if _topics_from(parsed) is not None:
            return parsed
        nested = _nested_resource_payload(parsed)
        if _topics_from(nested) is not None:
            return nested
    try:
        parsed = tool_response if isinstance(tool_response, dict) else json.loads(tool_response)
    except Exception:
        return None
    return parsed if _topics_from(parsed) is not None else None


def _extract_topics(tool_response):
    """The vocabulary's `topics` list, or None when the response carries no vocabulary."""
    return _topics_from(_vocab_payload(tool_response))


def _normalize_default_kb(raw):
    """`default_kb` as the vocabulary advertises it -- `{kb, source, unavailable?}` where `source`
    is "connection" (a human chose this KB for the connection) or "home" -- reduced to exactly
    those fields, or None for anything else (an older server that advertises none, or a shape this
    hook does not know). None is cached as ABSENCE: a server rolled back to one with no `default_kb`
    must stop earning whatever the cached value used to allow (routing_guard.py reads it)."""
    if not isinstance(raw, dict):
        return None
    kb = raw.get("kb")
    source = raw.get("source")
    if not isinstance(kb, str) or not kb or source not in ("connection", "home"):
        return None
    normalized = {"kb": kb, "source": source}
    unavailable = raw.get("unavailable")
    if isinstance(unavailable, str) and unavailable:
        normalized["unavailable"] = unavailable
    return normalized


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

    # The server advertises the current plugin version on every success envelope; cache it so the
    # NEXT session's SessionStart can compare without a network call (see plugin_update.py).
    plugin_latest = plugin_update.latest_from_blocks(_decoded_blocks(tool_response))
    if plugin_latest is not None:
        server_updates["pluginLatest"] = plugin_latest

    if is_vocab_read:
        vocab = _vocab_payload(tool_response)
        topics = _topics_from(vocab)
        if topics is not None:
            # Cached beside the topics, from the same read, and overwritten (never merged) each
            # time: the most recent observation is the truth.
            server_updates["defaultKb"] = _normalize_default_kb(vocab.get("default_kb"))
            normalized = _normalize_topics(topics)
            server_updates["topics"] = normalized
            server_updates["vocabFetchedAt"] = state_mod.now_iso()
            server_updates["undescribedTopics"] = [
                t["slug"] for t in normalized if not t["description"]
            ]

    incr_reads = _is_read_call(tool_name, is_vocab_read)

    incr_publishes = False
    if _is_publish_call(tool_name):
        success = _publish_outcome(tool_response)
        if success is not None:
            incr_publishes = True
            server_updates["lastPublishAt"] = state_mod.now_iso()
            # The slug comes from the success envelope itself rather than from a regex sweep over
            # the response: a refusal carries the item's slug too (revConflictError /
            # baseRevRequiredError both name it), so "the first slug anywhere in the response"
            # would happily record a write that never happened.
            server_updates["lastPublishSlug"] = _written_slug(success)
            if _is_append_call(tool_name) or _batch_landed_an_append(
                tool_name, tool_input, tool_response
            ):
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
            # pluginLatest is overwritten, never max()ed: the most recent observation is the
            # truth (a rolled-back server advertising an older version must stop the nudge).
            for field in ("topics", "vocabFetchedAt", "undescribedTopics", "lastPublishAt",
                          "lastPublishSlug", "lastAppendAt", "pluginLatest", "defaultKb"):
                if field in server_updates:
                    if server_updates[field] is None:
                        server.pop(field, None)  # an observed ABSENCE clears the cache
                    else:
                        server[field] = server_updates[field]

        if effect["session_vocab_rev"] is not None or effect["incr_reads"] or effect["incr_publishes"]:
            session = _touch_session(state, effect["session_id"])
            if effect["session_vocab_rev"] is not None:
                last_seen = session.get("lastSeenVocabRev")
                last_seen = last_seen if isinstance(last_seen, dict) else {}
                bucket = last_seen.get(effect["server_key"])
                bucket = dict(bucket) if isinstance(bucket, dict) else {}
                bucket[DEFAULT_KB_SLUG] = max(
                    effect["session_vocab_rev"], _as_counter(bucket.get(DEFAULT_KB_SLUG))
                )
                last_seen[effect["server_key"]] = bucket
                session["lastSeenVocabRev"] = last_seen
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
    scoped to tool_response only -- never tool_input, mirroring _envelope_candidates' scoping
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
    # Addressed to whoever just published -- normally kb-scribe, in whose context this fires
    # (PostToolUse runs where the tool call was made), which is also the agent that can describe
    # a topic it just minted without another round trip to the primary. ONE call for all of them:
    # a support KB that mints nine integrations from a single case used to be asked for nine
    # publish_topic calls; the batch form is `topics: [{slug, description}, ...]`.
    return (
        "%s %s %s auto-minted without descriptions -- describe %s in this same pass with ONE "
        "publish_topic call, topics: [{slug, description}, ...], a real one-line description each "
        "so the vocabulary stays legible; advisory, once per topic per session."
        % (
            "topics" if plural else "topic",
            ", ".join(slugs),
            "were" if plural else "was",
            "them all" if plural else "it",
        )
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
    # `organic_topics_minted` is a WRITE warning. _find_mint_warning recurses into JSON-parsable
    # strings, so without this gate a `get` whose body QUOTES such a warning would emit the
    # follow-up and write the quoted slug into undescribedTopics.
    if not _is_publish_call(tool_name):
        return None

    slugs = _extract_minted_slugs(raw_text, payload.get("tool_response"))
    if not slugs:
        return None

    fresh = _apply_mint_followup(_server_key(), sid, slugs)
    if not fresh:
        return None
    return _mint_message(fresh)


def plugin_update_nudge(payload):
    """Second extension to the harvest pass: when this gitian response advertises a
    `plugin_latest` newer than the installed manifest, return the update nudge
    (plugin_update.nudge_for owns the comparison and the once-a-day bound). None otherwise --
    including a non-gitian call, a call made inside a subagent, an error envelope (never stamped)
    or a current install."""
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if not _is_gitian_call(payload.get("tool_name"), tool_input):
        return None
    # Inside a subagent the payload carries `agent_id`. Its context reaches nobody who can act on
    # "tell the user", so stay silent there -- harvest() has already cached the version, and the
    # next SessionStart delivers it to the primary.
    if payload.get("agent_id"):
        return None
    latest = plugin_update.latest_from_blocks(_decoded_blocks(payload.get("tool_response")))
    if latest is None:
        return None
    return plugin_update.nudge_for(latest)


def main():
    raw_text, payload = _parse_stdin()
    effect = harvest(raw_text, payload)
    apply_effect(effect)

    parts = []
    for build_context in (lambda: mint_followup(raw_text, payload), lambda: plugin_update_nudge(payload)):
        try:
            text = build_context()
        except Exception:
            text = None  # one nudge failing must not swallow the other
        if text:
            parts.append(text)
    context = "\n\n".join(parts)
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
