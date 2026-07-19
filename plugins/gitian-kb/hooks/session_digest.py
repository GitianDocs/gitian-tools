#!/usr/bin/env python3
"""session_digest.py -- SessionStart source-profile text for the gitian-kb plugin's nudge layer.

Invoked by session-context.sh as `python3 session_digest.py <source> <session_id>` on every
SessionStart. Computes the one bit of source-profile-dependent text that isn't part of the
otherwise-static context session-context.sh already builds:

  - startup / clear / compact: a vocab digest -- a header line plus up to MAX_DIGEST_LINES
    "- slug - description (degree N)" lines (empty description -> "(no description yet)"),
    plus a "...and N more" line when the cache holds more topics than that. Omitted entirely
    (not even a header) when the cache has no topics for this server yet.
  - resume: zero, one, or both of -- (a) a line noting the server's vocab revision moved past
    what this session last saw, with instruction to re-read gitian-kb://vocab before the next
    publish, and (b) a line noting the session record is stale (>STALE_SESSION_HOURS old), as a
    nudge that the journal is a per-day running record and days may have rolled over. No digest
    on resume -- flags survive a resume (session-context.sh never bumps the epoch for it), so a
    full digest replay would be redundant with what startup/clear/compact already showed.

An unrecognized/missing source is treated like startup. Every source, after computing its text,
best-effort stamps sessions.<sid>.lastSeenVocabRev to the cache's current vocabRev (monotonic:
never regresses an already-higher value) -- this session has now seen the digest/delta, so its
notion of "vocab I've seen" should track the cache.

State is read via state.load() unlocked (a point-in-time read for display text needs no lock);
the one write here goes through state.with_lock(), same read-modify-write shape as state.py's
own mutating subcommands (see _record_seen_vocab_rev). `_server_key` mirrors harvest.py's helper
of the same name (same env override, same default, same state key) rather than importing it --
harvest.py doesn't export it either; it's a small enough contract to duplicate.

Output contract: a single already-JSON-escaped text fragment -- real newlines rendered as the
literal two-character `\\n` escape sequence, matching session-context.sh's own convention of
building its context string out of literal `\n` sequences rather than actual newline bytes --
ready for direct concatenation onto the additionalContext value session-context.sh is building.
Empty stdout (no trailing newline of its own either) when there is nothing to add.

Fail-open, silent-always: any exception anywhere is swallowed by the top-level guard in
`__main__` below and the process always exits 0 with no partial output -- session-context.sh's
own static context (RAG directive included) is built independently of this script and is never
affected by a failure here.

Run directly: python3 session_digest.py startup sess-123
Tests: plugins/gitian-kb/hooks/tests/test_session_context.py (drives session-context.sh end to
end, which in turn shells out to this script).
"""

import json
import os
import sys
from datetime import datetime, timezone

# session_digest.py always runs as a script file (never `python3 -c ...`), so Python has already
# put its own directory at sys.path[0] -- `import state` resolves state.py as a sibling module,
# same as harvest.py does (see its own docstring for the same note).
import state as state_mod

MAX_DIGEST_LINES = 25
STALE_SESSION_HOURS = 12
KNOWN_SOURCES = ("startup", "resume", "clear", "compact")


def _server_key():
    """"${GITIAN_KB_URL:-https://gitian.dev}/api/mcp" -- mirrors harvest.py's _server_key so
    every hook lands on the same state key."""
    base = os.environ.get("GITIAN_KB_URL")
    if not base:
        base = "https://gitian.dev"
    return base + "/api/mcp"


def _parse_iso(value):
    """Best-effort ISO-8601 parse (accepts 'Z' or explicit offsets); None on anything unparsable.
    Mirrors state.py's own private _parse_iso -- duplicated rather than imported since it isn't
    part of state.py's documented importable surface."""
    if not isinstance(value, str) or not value:
        return None
    try:
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _as_number(value):
    """value if it's a real int/float (bool excluded), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _fmt_number(value):
    """Render a stored numeric field without a spurious '.0' when it's a whole float."""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value)


def _escape(text):
    """JSON-escape TEXT for direct embedding in a JSON string value -- real newlines become the
    literal two-char `\\n` escape, quotes/backslashes escaped, matching how session-context.sh's
    own json_escape() prepares dynamic values before interpolation."""
    return json.dumps(text)[1:-1]


def _topic_line(topic):
    slug = topic.get("slug")
    description = topic.get("description") or "(no description yet)"
    degree = _as_number(topic.get("degree"))
    return "- %s - %s (degree %s)" % (slug, description, _fmt_number(degree if degree is not None else 0))


def _digest_text(server):
    """The vocab digest block, or None when the cache has no topics for this server at all --
    no placeholder text in that case, the tail is simply absent."""
    topics = server.get("topics")
    if not isinstance(topics, list):
        return None
    valid = [t for t in topics if isinstance(t, dict) and isinstance(t.get("slug"), str) and t.get("slug")]
    if not valid:
        return None

    vocab_rev = _as_number(server.get("vocabRev"))
    fetched_at = server.get("vocabFetchedAt") or "unknown"
    lines = [
        "KB vocab digest (as of vocab_rev %s, harvested %s):"
        % (_fmt_number(vocab_rev if vocab_rev is not None else 0), fetched_at)
    ]

    shown = valid[:MAX_DIGEST_LINES]
    lines.extend(_topic_line(topic) for topic in shown)

    remaining = len(valid) - len(shown)
    if remaining > 0:
        lines.append("...and %d more" % remaining)

    return "\n".join(lines)


def _resume_text(server, session):
    """Resume-only delta/staleness lines -- zero, one, or both, each independent of the other."""
    lines = []

    cache_rev = _as_number(server.get("vocabRev"))
    last_seen = _as_number(session.get("lastSeenVocabRev"))
    # last_seen is None the first time this session has ever seen a vocab_rev at all -- there is
    # no prior baseline to say it "moved" from, so stay silent rather than print a confusing
    # "None -> 5" line.
    if cache_rev is not None and last_seen is not None and cache_rev > last_seen:
        lines.append(
            "KB vocabulary moved since you last looked (vocab_rev %s -> %s) -- re-read "
            "gitian-kb://vocab before your next publish." % (_fmt_number(last_seen), _fmt_number(cache_rev))
        )

    updated_at = _parse_iso(session.get("updatedAt"))
    if updated_at is not None:
        age_hours = (datetime.now(timezone.utc) - updated_at).total_seconds() / 3600.0
        if age_hours > STALE_SESSION_HOURS:
            lines.append(
                "This session's KB activity is over %dh old -- the journal is a per-day "
                "running record, so days may have rolled over since." % STALE_SESSION_HOURS
            )

    if not lines:
        return None
    return "\n".join(lines)


def _record_seen_vocab_rev(sid, cache_rev):
    """Best-effort: stamp sessions.<sid>.lastSeenVocabRev = cache_rev (monotonic max, never a
    regression) under the state lock -- the same read-modify-write shape as state.py's own
    mutating subcommands. No-op without a sid or a known cache_rev."""
    if not sid or cache_rev is None:
        return
    path = state_mod.state_path()

    def mutate():
        state = state_mod.load(path)
        sessions = state.setdefault("sessions", {})
        session = sessions.get(sid)
        if not isinstance(session, dict):
            session = {}
            sessions[sid] = session
        state_mod.ensure_session_shape(session)
        current = _as_number(session.get("lastSeenVocabRev"))
        session["lastSeenVocabRev"] = cache_rev if current is None else max(cache_rev, current)
        session["updatedAt"] = state_mod.now_iso()
        state_mod.save(path, state_mod.finalize(state))

    state_mod.with_lock(path, mutate)


def build(source, sid):
    """Pure-ish: reads current state (no lock -- see module docstring) and returns the text to
    append, or None. The one side effect (recording lastSeenVocabRev) happens here too, since
    "has this session now seen the digest/delta" is exactly what this function just decided."""
    if source not in KNOWN_SOURCES:
        source = "startup"

    state = state_mod.load()
    servers = state.get("servers")
    servers = servers if isinstance(servers, dict) else {}
    server = servers.get(_server_key())
    server = server if isinstance(server, dict) else {}

    sessions = state.get("sessions")
    sessions = sessions if isinstance(sessions, dict) else {}
    session = sessions.get(sid) if sid else None
    session = session if isinstance(session, dict) else {}

    if source == "resume":
        text = _resume_text(server, session)
    else:
        text = _digest_text(server)

    _record_seen_vocab_rev(sid, _as_number(server.get("vocabRev")))

    return text


def main(argv):
    source = argv[0] if len(argv) > 0 else ""
    sid = argv[1] if len(argv) > 1 else ""
    text = build(source, sid)
    if text:
        # The leading blank-line separator must be the literal two-char `\n` escape sequence
        # TWICE (four characters: \, n, \, n) -- matching session-context.sh's own convention of
        # building its context out of literal `\n` sequences rather than real newline bytes (a
        # real newline byte embedded in a JSON string is invalid; the sh script relies on every
        # section separator already being pre-escaped text, not a control character).
        sys.stdout.write("\\n\\n" + _escape(text))


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit, never partial output
    sys.exit(0)
