#!/usr/bin/env python3
"""state.py -- shared state substrate for the gitian-kb plugin's nudge layer.

Single JSON file (schema v1) tracking, per MCP server URL, a cached vocab snapshot and
publish/append timestamps, and per Claude Code session, discipline counters/flags used to
decide whether a nudge should fire. Every write is a lock-guarded read-modify-write: an
exclusive fcntl.flock on "<statefile>.lock" wraps load -> mutate -> finalize (normalize shape,
prune stale sessions) -> atomic tmp-file + os.replace save.

Fail-open contract: this script NEVER raises past main() and NEVER exits non-zero. A missing
or corrupt state file is treated as empty ({}) and silently rebuilt into a valid v1 file on the
next write. A malformed invocation (bad subcommand, missing args, unreadable stdin) produces no
stdout and a clean exit(0) -- callers (POSIX sh hooks) can pipe this into their own logic without
ever needing to check an exit code.

CLI subcommands (see plugins/gitian-kb/hooks/lib-state.sh for the sh wrappers):
  get DOTPATH                   -> raw value at the dotted path, or nothing if absent
  merge                         <- stdin JSON, deep-merged into state (dicts recurse,
                                    scalars/arrays replace); silent
  incr SID COUNTER [DELTA=1]    -> silent; bumps sessions.SID.COUNTER by DELTA
  flag-once SID FLAG            -> prints "fire" iff this call set the flag (first time only)
  bump-epoch SID                -> silent; see bump_epoch() for exactly what resets
  status                        -> human-readable summary, never crashes on empty state
  dump                          -> the full normalized state as one line of JSON (debug/tests)

Also importable (sibling hook glue can `import state` directly rather than shelling out):
  state_path, now_iso, deep_merge, load, save, with_lock, ensure_session_shape,
  default_session, prune_sessions, finalize
"""

import fcntl
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = 1
DEFAULT_STATE_PATH = os.path.join("~", ".claude", "gitian-kb", "state.json")
MAX_SESSION_AGE_DAYS = 7

_SESSION_DEFAULTS = {
    "epoch": 0,
    "flags": {},
    "gitianReads": 0,
    "edits": 0,
    "publishes": 0,
    "lastSeenVocabRev": None,
    "lintHashes": [],
    "mintPrompted": [],
}


def state_path():
    """Resolve the state file path: GITIAN_KB_STATE_FILE env override, else the documented default."""
    return os.environ.get("GITIAN_KB_STATE_FILE") or os.path.expanduser(DEFAULT_STATE_PATH)


def now_iso():
    """Current UTC time as an ISO-8601 string with a literal 'Z' offset (matches stored timestamps)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value):
    """Best-effort ISO-8601 parse (accepts 'Z' or explicit offsets); None on anything unparsable."""
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


def deep_merge(base, incoming):
    """Dicts recurse key-by-key; anything else (scalars, lists, dict-over-non-dict) replaces outright."""
    if isinstance(base, dict) and isinstance(incoming, dict):
        result = dict(base)
        for key, value in incoming.items():
            result[key] = deep_merge(result[key], value) if key in result else value
        return result
    return incoming


def _empty_state():
    return {"schemaVersion": SCHEMA_VERSION, "servers": {}, "sessions": {}}


def load(path=None):
    """Read + normalize the state file. Missing file or invalid JSON/shape -> a fresh empty v1 state."""
    path = path or state_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state root is not an object")
    except Exception:
        data = {}
    if "schemaVersion" not in data:
        data["schemaVersion"] = SCHEMA_VERSION
    if not isinstance(data.get("servers"), dict):
        data["servers"] = {}
    if not isinstance(data.get("sessions"), dict):
        data["sessions"] = {}
    return data


def save(path, state):
    """Atomic write: tmp file in the same directory, then os.replace over the target."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def with_lock(path, fn):
    """Hold an exclusive flock on "<path>.lock" for the duration of fn() (a read-modify-write unit)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    lock_path = path + ".lock"
    with open(lock_path, "a+") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        try:
            return fn()
        finally:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def ensure_session_shape(session):
    """Fill in any missing v1 session fields with their defaults, in place. Never overwrites present keys."""
    for key, default in _SESSION_DEFAULTS.items():
        if key not in session:
            if isinstance(default, list):
                session[key] = []
            elif isinstance(default, dict):
                session[key] = {}
            else:
                session[key] = default
    return session


def default_session():
    session = ensure_session_shape({})
    session["updatedAt"] = now_iso()
    return session


def prune_sessions(state, max_age_days=MAX_SESSION_AGE_DAYS):
    """Drop sessions whose updatedAt is older than max_age_days. Unparsable/missing updatedAt is left alone
    (never guess-delete on ambiguous data)."""
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    stale = []
    for sid, sess in sessions.items():
        if not isinstance(sess, dict):
            continue
        dt = _parse_iso(sess.get("updatedAt"))
        if dt is not None and dt < cutoff:
            stale.append(sid)
    for sid in stale:
        del sessions[sid]


def finalize(state):
    """Normalize shape + schemaVersion and prune stale sessions. Always run immediately before save()."""
    if not isinstance(state.get("servers"), dict):
        state["servers"] = {}
    if not isinstance(state.get("sessions"), dict):
        state["sessions"] = {}
    state["schemaVersion"] = SCHEMA_VERSION
    prune_sessions(state)
    return state


def _touch_session(state, sid):
    """Get-or-create sessions[sid] with full v1 shape; caller stamps updatedAt once its edit is applied."""
    sessions = state.setdefault("sessions", {})
    session = sessions.get(sid)
    if not isinstance(session, dict):
        session = {}
        sessions[sid] = session
    ensure_session_shape(session)
    return session


# --- subcommands ---------------------------------------------------------------------------------


def cmd_get(args):
    if not args:
        return
    dotpath = args[0]
    if not dotpath:
        return
    node = load()
    for part in dotpath.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return
    if node is None:
        return
    sys.stdout.write(node if isinstance(node, str) else json.dumps(node))
    sys.stdout.write("\n")


def cmd_merge(args):
    raw = sys.stdin.read()
    try:
        doc = json.loads(raw)
        if not isinstance(doc, dict):
            doc = {}
    except Exception:
        doc = {}

    path = state_path()

    def mutate():
        state = load(path)
        merged = deep_merge(state, doc)
        touched = doc.get("sessions")
        if isinstance(touched, dict):
            for sid in touched:
                session = merged.get("sessions", {}).get(sid)
                if isinstance(session, dict):
                    ensure_session_shape(session)
                    session["updatedAt"] = now_iso()
        save(path, finalize(merged))

    with_lock(path, mutate)


def cmd_incr(args):
    if len(args) < 2:
        return
    sid, counter = args[0], args[1]
    if not sid or not counter:
        return
    delta = 1
    if len(args) >= 3:
        try:
            delta = int(args[2])
        except Exception:
            delta = 1

    path = state_path()

    def mutate():
        state = load(path)
        session = _touch_session(state, sid)
        current = session.get(counter, 0)
        if not isinstance(current, (int, float)) or isinstance(current, bool):
            current = 0
        session[counter] = current + delta
        session["updatedAt"] = now_iso()
        save(path, finalize(state))

    with_lock(path, mutate)


def cmd_flag_once(args):
    if len(args) < 2:
        return
    sid, flag = args[0], args[1]
    if not sid or not flag:
        return

    path = state_path()

    def mutate():
        state = load(path)
        session = _touch_session(state, sid)
        flags = session.setdefault("flags", {})
        if flags.get(flag) is True:
            return False  # already fired -- no write needed
        flags[flag] = True
        session["updatedAt"] = now_iso()
        save(path, finalize(state))
        return True

    if with_lock(path, mutate):
        sys.stdout.write("fire\n")


def cmd_bump_epoch(args):
    if not args:
        return
    sid = args[0]
    if not sid:
        return

    path = state_path()

    def mutate():
        state = load(path)
        session = _touch_session(state, sid)
        session["epoch"] = int(session.get("epoch") or 0) + 1
        session["flags"] = {}
        session["lintHashes"] = []
        session["mintPrompted"] = []
        session["gitianReads"] = 0
        session["edits"] = 0
        session["publishes"] = 0
        # lastSeenVocabRev deliberately survives an epoch bump.
        session["updatedAt"] = now_iso()
        save(path, finalize(state))

    with_lock(path, mutate)


def _humanize_age(iso_value):
    dt = _parse_iso(iso_value)
    if dt is None:
        return "never"
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%dm ago" % int(seconds // 60)
    if seconds < 86400:
        return "%dh ago" % int(seconds // 3600)
    return "%dd ago" % int(seconds // 86400)


def _most_recent_session(sessions):
    best_sid, best_session, best_dt = None, None, None
    for sid, session in sessions.items():
        if not isinstance(session, dict):
            continue
        dt = _parse_iso(session.get("updatedAt"))
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_sid, best_session, best_dt = sid, session, dt
    return best_sid, best_session or {}


def cmd_status(_args):
    state = load()
    lines = []

    servers = state.get("servers") or {}
    if servers:
        lines.append("servers:")
        for url in sorted(servers.keys()):
            server = servers[url] if isinstance(servers[url], dict) else {}
            topics = server.get("topics")
            topic_count = len(topics) if isinstance(topics, list) else 0
            undescribed = server.get("undescribedTopics")
            undescribed_str = (
                ", ".join(undescribed) if isinstance(undescribed, list) and undescribed else "none"
            )
            vocab_rev = server.get("vocabRev")
            last_publish = server.get("lastPublishAt") or "never"
            last_append = server.get("lastAppendAt") or "never"
            slug = server.get("lastPublishSlug")
            lines.append(
                "  - %s: vocabRev=%s, fetched %s, topics=%d, undescribed=[%s], "
                "last publish=%s%s, last append=%s"
                % (
                    url,
                    vocab_rev if vocab_rev is not None else "?",
                    _humanize_age(server.get("vocabFetchedAt")),
                    topic_count,
                    undescribed_str,
                    last_publish,
                    (" (%s)" % slug) if slug else "",
                    last_append,
                )
            )
    else:
        lines.append("servers: none tracked")

    sessions = state.get("sessions") or {}
    lines.append("sessions: %d tracked" % len(sessions))
    current_sid, current = _most_recent_session(sessions)
    if current_sid is not None:
        flags = current.get("flags") if isinstance(current.get("flags"), dict) else {}
        fired = sorted(name for name, value in flags.items() if value)
        lines.append(
            "current session %s: epoch=%s, gitianReads=%s, edits=%s, publishes=%s, flags fired=[%s]"
            % (
                current_sid,
                current.get("epoch", 0),
                current.get("gitianReads", 0),
                current.get("edits", 0),
                current.get("publishes", 0),
                ", ".join(fired) if fired else "none",
            )
        )

    sys.stdout.write("\n".join(lines))
    sys.stdout.write("\n")


def cmd_dump(_args):
    sys.stdout.write(json.dumps(load()))
    sys.stdout.write("\n")


_SUBCOMMANDS = {
    "get": cmd_get,
    "merge": cmd_merge,
    "incr": cmd_incr,
    "flag-once": cmd_flag_once,
    "bump-epoch": cmd_bump_epoch,
    "status": cmd_status,
    "dump": cmd_dump,
}


def main(argv):
    if not argv:
        return
    handler = _SUBCOMMANDS.get(argv[0])
    if handler is None:
        return
    handler(argv[1:])


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit
    sys.exit(0)
