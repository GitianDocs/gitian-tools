#!/usr/bin/env python3
"""ledger.py -- the gitian-kb nudge layer's activity ledger and the checkpoints that re-arm it.

Until 0.33.0 every completion nudge latched once per (session, epoch), and the Stop reminder also
went quiet for good after the session's FIRST publish. A 14-hour session with nine merges, a prod
release and four standing owner rules therefore heard nothing after its first hour
([[kb-nudge-checkpoints-plan]]). This module replaces the latch with a record and a cover point:

  - the LEDGER (`sessions.<sid>.ledger`) records what the session did that may owe a KB write:
    `events` (commit, merge, release, pr, ruling) and `edits` (per-minute buckets), each stamped
    with the repo it happened in. A subagent's hooks fire under the parent's session id, so an
    implementer's commits land in the same ledger as the primary's.
  - CHECKPOINTS say how far that activity is handled. A kb-scribe dispatch sets `briefAt` (pending:
    it covers what came before it for BRIEF_GRACE, so a background scribe is not nagged about
    before its publish lands); a content write sets `checkpointAt` and prunes what it covers.
  - the WATCH (`sessions.<sid>.watch`) holds pull requests to poll, because a merge made by
    auto-merge or by a person on GitHub leaves no local command for any hook to see.
  - `sessions.<sid>.nudges.at` is the last time a checkpoint nudge showed the ledger; only activity
    newer than it can fire the next one.

Nudges are evaluated ONLY at checkpoints, never right after the event: a compliant agent briefs
the scribe within the turn, so by the next checkpoint its activity is covered and the session stays
silent -- the release gate in tests/test_invariant.py. Checkpoints are priced by what they cost the
agent: a Stop block re-runs a whole turn at the session's full context, so it is reserved for
completion points (the session's first work, and merges / releases at most every
COMPLETION_FLOOR, which collapses a burst into one block); everything else rides along as a few
lines of context on the user's next prompt (`checkpoint_due(..., at_prompt=True)`) or at
SessionStart, costing no turn at all.

Everything here is a pure function over a session dict and an aware UTC `now`, except the repo
helpers (a `.git` walk-up plus `git` subprocesses with a 2 s timeout) and `mutate_session`, the one
locked read-modify-write the hooks go through. Fail-open like every sibling: the CLI never raises,
never prints a traceback, always exits 0.

CLI (for the sh hooks):
  python3 ledger.py edit SID [PATH]          count one file edit, attributed to PATH's repo
  python3 ledger.py watch SID PR_URL [WHY]   watch a pull request (default why: branch)
  python3 ledger.py brief SID                record a kb-scribe brief

Tests: plugins/gitian-kb/hooks/tests/test_ledger.py.
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Always run as a script or imported by a sibling hook script, so this directory is already on
# sys.path (the same convention harvest.py documents).
import state as state_mod

EDIT_THRESHOLD = 3
COMPLETION_FLOOR = timedelta(minutes=30)
WORK_COOLDOWN = timedelta(hours=2)
RULING_HINT_FLOOR = timedelta(minutes=10)
BRIEF_GRACE = timedelta(minutes=30)
SCRIBE_LAG = timedelta(minutes=10)
POLL_INTERVAL = timedelta(minutes=5)
WATCH_TTL = timedelta(hours=72)

# Caps keep a long session's state small: every hook reads and rewrites the whole state file.
MAX_EVENTS = 40
MAX_EDIT_BUCKETS = 60
MAX_WATCH = 12
MAX_SCRIBE_IDS = 5
MAX_BRIEFED_PRS = 30
STALE_MERGE_SLACK = timedelta(minutes=10)
MAX_POLL_ENTRIES = 20

MAX_TEXT = 160  # stored excerpt / title length
TITLE_SHOWN = 60
RULING_SHOWN = 140
MAX_GROUPS_SHOWN = 6
MAX_ITEMS_SHOWN = 3
MAX_RULINGS_SHOWN = 3

EVENT_KINDS = ("commit", "merge", "release", "pr", "ruling")

_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")  # always fullmatch: `$` admits a trailing newline
_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,200}")
# A pull request a brief names: `#650`, `owner/name#650`, or a `/pull/650` URL.
_BRIEF_PR_RE = re.compile(r"(?:#|/pull/)(\d{1,7})\b")
_PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)")
_SCP_REMOTE_RE = re.compile(r"^[A-Za-z0-9_.-]+@[^:/]+:(.+)$")
_URL_REMOTE_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^/]+/(.+)$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")


# --- time ----------------------------------------------------------------------------------------


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    """Best-effort ISO-8601 parse to an aware UTC datetime; None on anything unparsable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        dt = datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _latest(*values):
    present = [v for v in values if v is not None]
    return max(present) if present else None


# --- shape: every reader normalizes first, so a corrupt or older state file reads as empty ------


def _is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _valid_event(event):
    return (
        isinstance(event, dict)
        and event.get("kind") in EVENT_KINDS
        and parse_iso(event.get("at")) is not None
    )


def _valid_bucket(bucket):
    return (
        isinstance(bucket, dict)
        and parse_iso(bucket.get("at")) is not None
        and _is_count(bucket.get("n"))
    )


def _valid_watch(entry):
    if not isinstance(entry, dict) or _clean_repo(entry.get("repo")) is None:
        return False
    if parse_iso(entry.get("addedAt")) is None:
        return False
    number, branch = entry.get("number"), entry.get("branch")
    return _is_count(number) or (isinstance(branch, str) and bool(_BRANCH_RE.fullmatch(branch)))


def ledger_of(session):
    """The session's ledger, normalized in place (malformed entries dropped, never guessed at)."""
    led = session.get("ledger")
    if not isinstance(led, dict):
        led = {}
        session["ledger"] = led
    events = led.get("events")
    led["events"] = [e for e in events if _valid_event(e)] if isinstance(events, list) else []
    edits = led.get("edits")
    led["edits"] = [b for b in edits if _valid_bucket(b)] if isinstance(edits, list) else []
    ids = led.get("scribeIds")
    ids = [i for i in ids if isinstance(i, str) and i] if isinstance(ids, list) else []
    led["scribeIds"] = ids[-MAX_SCRIBE_IDS:]
    if not _is_count(led.get("dropped")):
        led["dropped"] = 0
    for key in ("checkpointAt", "briefAt"):
        if key in led and parse_iso(led.get(key)) is None:
            del led[key]
    if "transcript" in led and not isinstance(led.get("transcript"), str):
        del led["transcript"]
    briefed = led.get("briefedPRs")
    if briefed is not None:
        briefed = [b for b in briefed if isinstance(b, dict) and _is_count(b.get("n"))
                   and parse_iso(b.get("at")) is not None] if isinstance(briefed, list) else []
        led["briefedPRs"] = briefed[-MAX_BRIEFED_PRS:]
    return led


def nudges_of(session):
    nudges = session.get("nudges")
    if not isinstance(nudges, dict):
        nudges = {}
        session["nudges"] = nudges
    for key in ("at", "pollAt", "rulingAt"):
        if key in nudges and parse_iso(nudges.get(key)) is None:
            del nudges[key]
    return nudges


def watch_of(session):
    watch = session.get("watch")
    watch = [dict(e) for e in watch if _valid_watch(e)] if isinstance(watch, list) else []
    session["watch"] = watch
    return watch


# --- text ----------------------------------------------------------------------------------------


def clip(text, limit):
    """One line, at most `limit` chars, cut on a word boundary. Titles and commit subjects come
    from git and GitHub, so they are untrusted: control characters and newlines go, and double
    quotes become single ones because the renderer quotes them."""
    if not isinstance(text, str):
        return None
    flat = _SPACE_RE.sub(" ", _CONTROL_RE.sub(" ", text)).strip().replace('"', "'")
    if not flat:
        return None
    if len(flat) <= limit:
        return flat
    cut = flat[: limit - 3]
    space = cut.rfind(" ")
    if space >= limit // 2:
        cut = cut[:space]
    return cut.rstrip() + "..."


def _clean_repo(repo):
    if not isinstance(repo, str):
        return None
    parts = repo.split("/")
    if len(parts) != 2 or not all(_SEGMENT_RE.fullmatch(p) for p in parts):
        return None
    return repo


def _clean_label(label):
    return clip(label, 60) if isinstance(label, str) else None


# --- recording -----------------------------------------------------------------------------------


def note_transcript(session, path):
    """Remember the session's transcript, so the /clear carry-over can name it to the scribe."""
    if isinstance(path, str) and path:
        ledger_of(session)["transcript"] = path


def record_event(session, now, kind, repo=None, where=None, ref=None, text=None, draft=False,
                 seen=None):
    """Append one event. Returns False for an unknown kind or for a repeat of an event still in the
    ledger -- a hook that fires twice for one command, or a merge the poll sees through both a
    number and a branch watch entry. A repeat with an EARLIER stamp lowers the stored one: a merge
    the session made itself carries GitHub's mergedAt, and that must win over the same merge seen
    through an `opened` entry at poll time (see _merge_stamp).

    `now` stamps `at`, WHEN IT HAPPENED, which is what a brief or a write covers. `seen`, when it
    differs, is when the ledger LEARNED of it (a poll after the merge), which is what "new since
    the last nudge" asks: a merge made before a nudge but only detected after it is still news."""
    if kind not in EVENT_KINDS:
        return False
    led = ledger_of(session)
    repo = _clean_repo(repo)
    where = None if repo else _clean_label(where)
    ref = clip(ref, 60)
    text = clip(text, MAX_TEXT)
    draft = bool(draft) and kind == "release"
    for event in led["events"]:
        if event.get("kind") != kind or event.get("repo") != repo:
            continue
        if ref is not None and event.get("ref") == ref and bool(event.get("draft")) == draft:
            if now < parse_iso(event["at"]):
                event["at"] = iso(now)
            return False
        if kind == "ruling" and text is not None and event.get("text") == text:
            return False
    event = {"at": iso(now), "kind": kind}
    if seen is not None and seen != now:
        event["seen"] = iso(seen)
    for key, value in (("repo", repo), ("where", where), ("ref", ref), ("text", text)):
        if value is not None:
            event[key] = value
    if draft:
        event["draft"] = True
    led["events"].append(event)
    overflow = len(led["events"]) - MAX_EVENTS
    if overflow > 0:
        del led["events"][:overflow]
        led["dropped"] += overflow
    return True


def record_edit(session, now, repo=None, where=None, n=1):
    """Count `n` file edits in the current minute's bucket for this repo. A bucket's `at` is its
    LATEST edit, so a checkpoint inside a minute never prunes an edit made after it."""
    if not _is_count(n):
        return
    led = ledger_of(session)
    repo = _clean_repo(repo)
    where = None if repo else _clean_label(where)
    stamp = iso(now)
    minute = stamp[:16]
    for bucket in reversed(led["edits"][-4:]):
        if bucket.get("min") == minute and bucket.get("repo") == repo and bucket.get("where") == where:
            bucket["n"] += n
            bucket["at"] = stamp
            return
    bucket = {"min": minute, "at": stamp, "n": n}
    if repo is not None:
        bucket["repo"] = repo
    if where is not None:
        bucket["where"] = where
    led["edits"].append(bucket)
    # Over the cap, fold the oldest bucket into the next one: the count survives (it is what the
    # threshold reads), only its attribution blurs.
    while len(led["edits"]) > MAX_EDIT_BUCKETS:
        oldest = led["edits"].pop(0)
        led["edits"][0]["n"] += oldest["n"]


def watch_key(entry):
    """`owner/name#650` or `owner/name@branch` -- the same key pr_poll.entry_key builds."""
    if entry.get("number"):
        return "%s#%d" % (entry.get("repo"), entry.get("number"))
    return "%s@%s" % (entry.get("repo"), entry.get("branch"))


# A re-watch of the same pull request upgrades its `why` along this order, never down: the
# session merging a PR it opened is a self-made merge (stamped with mergedAt), whatever came first.
_WHY_RANK = {"branch": 0, "opened": 0, "armed": 1, "merge": 2}


def add_watch(session, now, repo, number=None, branch=None, why="opened"):
    repo = _clean_repo(repo)
    if repo is None:
        return False
    number = number if _is_count(number) else None
    branch = branch if isinstance(branch, str) and _BRANCH_RE.fullmatch(branch) else None
    if number is None and branch is None:
        return False
    entry = {"repo": repo, "why": str(why)[:20], "addedAt": iso(now)}
    if number is not None:
        entry["number"] = number
    else:
        entry["branch"] = branch
    watch = watch_of(session)
    key = watch_key(entry)
    for existing in watch:
        if watch_key(existing) == key:
            if _WHY_RANK.get(entry["why"], 0) > _WHY_RANK.get(existing.get("why"), 0):
                existing["why"] = entry["why"]
            return False
    watch.append(entry)
    del watch[: max(0, len(watch) - MAX_WATCH)]
    return True


def due_watch(session, now):
    """Watch entries to poll now: everything, when any entry was never polled or POLL_INTERVAL
    has passed since the last poll; otherwise none. Expired entries are dropped first."""
    watch = watch_of(session)
    live = [e for e in watch if now - parse_iso(e["addedAt"]) < WATCH_TTL]
    session["watch"] = live
    if not live:
        return []
    last = parse_iso(nudges_of(session).get("pollAt"))
    never = any(parse_iso(e.get("polledAt")) is None for e in live)
    if not never and last is not None and now - last < POLL_INTERVAL:
        return []
    return [dict(e) for e in live[:MAX_POLL_ENTRIES]]


def _merge_stamp(session, entry, result, now):
    """When a merge happened, as far as covering it goes. GitHub's `mergedAt` is used when the agent
    demonstrably KNEW of the merge: it made it itself (a plain `gh pr merge`, watch why `merge`), or
    a brief written after the merge names the PR (`#N`, a `/pull/N` URL) -- the agent watched an
    armed auto-merge land and wrote it up. Then a brief written after the merge covers it. Any other
    merge happened on GitHub's schedule, unseen, and is stamped when the poll saw it: no earlier
    brief can have held it, and that unseen merge is exactly what the checkpoints exist to surface
    (the incident: auto-merges landing while the agent briefed about something else)."""
    merged = parse_iso(result.get("mergedAt"))
    if merged is None or merged > now:
        return now
    if entry.get("why") == "merge":
        return merged
    number = result.get("number") if _is_count(result.get("number")) else entry.get("number")
    for briefed in ledger_of(session).get("briefedPRs", []):
        if briefed.get("n") == number and (parse_iso(briefed.get("at")) or merged) > merged:
            return merged
    return now


def apply_poll(session, now, results, polled=None):
    """Fold one poll's results in: MERGED records a `merge` event (stamped by _merge_stamp) and
    unwatches, CLOSED unwatches, anything else (OPEN, unknown, a failed poll) keeps watching.
    `polled` is the list of entries handed to the poll; each is stamped `polledAt` whatever came
    back, so a failing `gh` is retried on the POLL_INTERVAL cadence rather than at every turn
    end."""
    results = results if isinstance(results, dict) else {}
    polled_keys = None if polled is None else {watch_key(e) for e in polled if isinstance(e, dict)}
    keep = []
    for entry in watch_of(session):
        if now - parse_iso(entry["addedAt"]) >= WATCH_TTL:
            continue
        key = watch_key(entry)
        if polled_keys is None or key in polled_keys:
            entry["polledAt"] = iso(now)
        result = results.get(key)
        state = result.get("state") if isinstance(result, dict) else None
        if state == "MERGED":
            merged = parse_iso(result.get("mergedAt"))
            if merged is not None and merged < parse_iso(entry["addedAt"]) - STALE_MERGE_SLACK:
                continue  # merged long before this watch began: a reused branch name's old PR
            number = result.get("number") if _is_count(result.get("number")) else entry.get("number")
            ref = ("#%d" % number) if number else "@" + entry.get("branch", "")
            record_event(
                session,
                _merge_stamp(session, entry, result, now),
                "merge",
                repo=entry["repo"],
                ref=ref,
                # A branch watch (SessionStart's open PR) may be someone else's PR: its title is
                # untrusted text that would reach the model under the hook's banner, so it is left
                # out. The session's own PRs keep theirs.
                text=None if entry.get("why") == "branch" else result.get("title"),
                seen=now,
            )
            continue
        if state == "CLOSED":
            continue
        keep.append(entry)
    session["watch"] = keep
    nudges_of(session)["pollAt"] = iso(now)


# --- checkpoints ---------------------------------------------------------------------------------


def _remember_scribe(led, agent_id):
    if isinstance(agent_id, str) and agent_id and agent_id not in led["scribeIds"]:
        led["scribeIds"].append(agent_id)
        del led["scribeIds"][: max(0, len(led["scribeIds"]) - MAX_SCRIBE_IDS)]


def _note_briefed_prs(led, now, text):
    """Remember which pull requests a brief names, so a merge the agent saw and wrote up is covered
    by that brief even though the poll only detects it afterwards (see _merge_stamp)."""
    if not isinstance(text, str) or not text:
        return
    numbers = {int(n) for n in _BRIEF_PR_RE.findall(text[:20000]) if int(n) > 0}
    if not numbers:
        return
    briefed = [b for b in led.get("briefedPRs", []) if isinstance(b, dict) and b.get("n") not in numbers]
    briefed.extend({"n": n, "at": iso(now)} for n in sorted(numbers))
    led["briefedPRs"] = briefed[-MAX_BRIEFED_PRS:]


def mark_brief(session, now, agent_id=None, text=None):
    """The primary just briefed kb-scribe: a PENDING checkpoint (see cover_point). Called when the
    dispatch STARTS (PreToolUse), so a foreground scribe's publishes, which land before the
    dispatch returns, already have a brief to confirm, and again when it returns (PostToolUse),
    which is the only point that knows the scribe's agent id."""
    led = ledger_of(session)
    led["briefAt"] = iso(now)
    _remember_scribe(led, agent_id)
    _note_briefed_prs(led, now, text)



def note_scribe_message(session, now, to, text=None):
    """A SendMessage to a live scribe is a follow-up brief; to anyone else it is nothing."""
    led = ledger_of(session)
    if isinstance(to, str) and to in led["scribeIds"]:
        led["briefAt"] = iso(now)
        _note_briefed_prs(led, now, text)
        return True
    return False


def apply_content_write(session, now, delegated, agent_id=None):
    """A KB content write landed. The primary's own write covers everything up to now. A
    subagent's (the scribe's) covers only what its brief could have held -- up to the latest brief,
    or SCRIBE_LAG back when no brief was seen -- because activity between the brief and a publish
    landing minutes later was never in it, and wiping that is exactly the silent loss this module
    exists to prevent."""
    led = ledger_of(session)
    if delegated:
        brief = parse_iso(led.get("briefAt"))
        point = brief if brief is not None and brief <= now else now - SCRIBE_LAG
        _remember_scribe(led, agent_id)
    else:
        point = now
    current = parse_iso(led.get("checkpointAt"))
    if current is None or point > current:
        led["checkpointAt"] = iso(point)
    prune(session)


def prune(session):
    led = ledger_of(session)
    point = parse_iso(led.get("checkpointAt"))
    if point is None:
        return
    kept = [e for e in led["events"] if parse_iso(e["at"]) > point]
    if len(kept) < len(led["events"]):
        led["dropped"] = 0  # overflow drops the OLDEST events, so they are covered too
    led["events"] = kept
    led["edits"] = [b for b in led["edits"] if parse_iso(b["at"]) > point]


def cover_point(session, now):
    """How far activity counts as handled: the last checkpoint, or a brief still inside its
    BRIEF_GRACE. A brief whose scribe never published stops covering once the grace runs out, so
    activity it promised to write resurfaces instead of vanishing."""
    led = ledger_of(session)
    brief = parse_iso(led.get("briefAt"))
    if brief is not None and (brief > now or now - brief > BRIEF_GRACE):
        brief = None
    return _latest(parse_iso(led.get("checkpointAt")), brief)


def _open(session, now, after=None):
    """Items no checkpoint covers (`at` past the cover point) and, with `after`, also new since
    then (`seen`, else `at`, past it)."""
    cover = cover_point(session, now)
    led = ledger_of(session)

    def keep(item):
        if cover is not None and parse_iso(item["at"]) <= cover:
            return False
        if after is None:
            return True
        return (parse_iso(item.get("seen")) or parse_iso(item["at"])) > after

    return [e for e in led["events"] if keep(e)], [b for b in led["edits"] if keep(b)]


def _is_completion(event):
    kind = event.get("kind")
    return kind == "merge" or (kind == "release" and not event.get("draft"))


def _qualifies(event):
    """Activity that can fire a checkpoint. A standing rule is not one: it gets its own immediate
    hint on the prompt that carried it (ruling_hint) and is listed while open, but never costs a
    turn by itself -- a false positive of a text heuristic must stay cheap."""
    return event.get("kind") == "commit" or _is_completion(event)


def _edit_total(buckets):
    return sum(b["n"] for b in buckets)


def has_open_qualifying(session, now):
    events, edits = _open(session, now)
    return any(_qualifies(e) for e in events) or _edit_total(edits) >= EDIT_THRESHOLD


def checkpoint_due(session, now, at_prompt=False):
    """Whether this checkpoint should show the ledger. Always requires open qualifying activity
    and something NEW since the last nudge. Then, measured from the quiet point (the latest nudge,
    brief or covering write):
      - none yet (a session that never wrote, briefed or was nudged): fire -- the original
        first-work reminder (pinned decision 4), at any checkpoint;
      - a new merge or non-draft release: fire once COMPLETION_FLOOR has passed (a burst of merges
        becomes one nudge, and a scribe just briefed gets time to land);
      - at a prompt only, a new commit, merge or release, or EDIT_THRESHOLD new edits, once
        WORK_COOLDOWN has passed: work is not worth a forced turn, so it waits for a context line
        on the user's next message, or rides along in the next completion block. Info-only items
        (an opened PR, a draft release, a standing rule) never re-arm anything."""
    if not has_open_qualifying(session, now):
        return False
    nudged = parse_iso(nudges_of(session).get("at"))
    new_events, new_edits = _open(session, now, after=nudged)
    new_completion = any(_is_completion(e) for e in new_events)
    new_work = (
        new_completion
        or any(e.get("kind") == "commit" for e in new_events)
        or _edit_total(new_edits) >= EDIT_THRESHOLD
    )
    if not new_work:
        return False
    led = ledger_of(session)
    quiet = _latest(nudged, parse_iso(led.get("checkpointAt")), parse_iso(led.get("briefAt")))
    if quiet is None:
        return True
    if new_completion and now - quiet >= COMPLETION_FLOOR:
        return True
    return at_prompt and now - quiet >= WORK_COOLDOWN


def stop_decision(session, now):
    """The Stop hook's question: a block costs a turn, so only completion points qualify."""
    return checkpoint_due(session, now, at_prompt=False)


def ruling_hint_due(session, now):
    """At most one standing-rule hint per RULING_HINT_FLOOR: a rule stated over several messages
    is one rule, and every hint is context the agent pays for."""
    last = parse_iso(nudges_of(session).get("rulingAt"))
    return last is None or now - last >= RULING_HINT_FLOOR


def mark_ruling_hinted(session, now):
    nudges_of(session)["rulingAt"] = iso(now)


def mark_nudged(session, now):
    nudges_of(session)["at"] = iso(now)


def reset_for_clear(session, now):
    """/clear empties the conversation, so the carry-over text is the last place this session's
    unwritten activity can be named; once shown, the ledger starts over. The watch and the known
    scribe ids survive: a pull request open before /clear is still open after it."""
    led = ledger_of(session)
    led["events"] = []
    led["edits"] = []
    led["dropped"] = 0
    led["checkpointAt"] = iso(now)
    led.pop("briefAt", None)


# --- rendering -----------------------------------------------------------------------------------


def _fmt_time(dt, now):
    if dt.date() == now.date():
        return dt.strftime("%H:%MZ")
    return dt.strftime("%Y-%m-%d %H:%MZ")


def since_label(session, now):
    cover = cover_point(session, now)
    if cover is None:
        events, edits = _open(session, now)
        stamps = [parse_iso(i["at"]) for i in events + edits]
        cover = min(stamps) if stamps else now
    return _fmt_time(cover, now)


def _group_label(item):
    if item.get("repo"):
        return item["repo"]
    if item.get("where"):
        return "%s (no GitHub remote)" % item["where"]
    return "other files"


def _listed(items, render):
    shown = [render(i) for i in items[-MAX_ITEMS_SHOWN:]]
    more = len(items) - len(shown)
    return ", ".join(shown) + (" +%d more" % more if more > 0 else "")


def _titled(ref, text):
    title = clip(text, TITLE_SHOWN)
    return '%s "%s"' % (ref, title) if title else ref


def render_lines(session, now):
    """The open activity as `- ...` lines: one per repo (most recent first), then the user's
    standing rules. Bounded, so a long session's nudge stays a few hundred characters."""
    events, edits = _open(session, now)
    groups = {}
    rulings = []

    def group(label, at):
        g = groups.setdefault(label, {"at": at, "merge": [], "release": [], "draft": [],
                                      "pr": [], "commit": [], "edits": 0})
        g["at"] = max(g["at"], at)
        return g

    for event in events:
        at = parse_iso(event["at"])
        if event["kind"] == "ruling":
            rulings.append(event)
            continue
        g = group(_group_label(event), at)
        key = "draft" if event["kind"] == "release" and event.get("draft") else event["kind"]
        g[key].append(event)
    for bucket in edits:
        group(_group_label(bucket), parse_iso(bucket["at"]))["edits"] += bucket["n"]

    lines = []
    ordered = sorted(groups.items(), key=lambda kv: kv[1]["at"], reverse=True)
    for label, g in ordered[:MAX_GROUPS_SHOWN]:
        parts = []
        if g["merge"]:
            parts.append("merged " + _listed(g["merge"], lambda e: _titled(e.get("ref", "a PR"), e.get("text"))))
        if g["release"]:
            parts.append("released " + _listed(g["release"], lambda e: e.get("ref", "a release")))
        if g["draft"]:
            parts.append("drafted release " + _listed(g["draft"], lambda e: e.get("ref", "?")))
        if g["pr"]:
            parts.append("opened " + _listed(g["pr"], lambda e: _titled(e.get("ref", "a PR"), e.get("text"))))
        if g["commit"]:
            count = len(g["commit"])
            parts.append(
                "%d commit%s (%s)"
                % (count, "" if count == 1 else "s",
                   _listed(g["commit"], lambda e: _titled(e.get("ref", "?"), e.get("text"))))
            )
        if g["edits"]:
            parts.append("%d file edit%s" % (g["edits"], "" if g["edits"] == 1 else "s"))
        if parts:
            lines.append("- %s: %s" % (label, "; ".join(parts)))
    hidden = len(ordered) - MAX_GROUPS_SHOWN
    if hidden > 0:
        lines.append("- and %d more repo%s" % (hidden, "" if hidden == 1 else "s"))
    for event in rulings[-MAX_RULINGS_SHOWN:]:
        lines.append('- the user: "%s"' % (clip(event.get("text"), RULING_SHOWN) or "(a standing rule)"))
    dropped = ledger_of(session)["dropped"]
    if dropped:
        lines.append("- (+%d older item%s not listed)" % (dropped, "" if dropped == 1 else "s"))
    return lines


# Every line of these is context an agent pays for, on every nudge: keep them short.
CHECKPOINT_TAIL = (
    "Whatever clears the meaningful-event bar goes in ONE kb-scribe brief (background; SendMessage "
    "a live scribe), each line's repo included. Nothing qualifies? Carry on: this re-arms only on "
    "new activity."
)


def _head(session, now):
    return "gitian-kb checkpoint: activity since %s that no KB write covers yet:" % since_label(
        session, now
    )


def stop_reason(session, now):
    return "\n".join([_head(session, now)] + render_lines(session, now) + [CHECKPOINT_TAIL])


def prompt_text(session, now):
    """The same checkpoint, delivered as context on the user's prompt instead of a blocked turn."""
    return stop_reason(session, now)


def ruling_hint(excerpt):
    return (
        'gitian-kb: this reads like a standing rule ("%s"). If it is one, put a memory for it in '
        "your next kb-scribe brief." % (clip(excerpt, RULING_SHOWN) or "...")
    )


# --- repos ---------------------------------------------------------------------------------------

_LOCATE_CACHE = {}


def normalize_remote(url):
    """`owner/name` from a git remote URL (scp-style `git@host:o/n.git`, or `https://`/`ssh://`
    host URLs), or None for anything else -- the same reduction session-context.sh applies, since
    both feed the `repo` a brief carries."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    match = _SCP_REMOTE_RE.match(url) or _URL_REMOTE_RE.match(url)
    if not match:
        return None
    path = match.group(1).rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return _clean_repo(path)


def _git(args, timeout=2.0):
    try:
        proc = subprocess.run(
            ["git"] + list(args),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0"),
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def _existing_dir(path):
    current = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(current):
        current = os.path.dirname(current)
    while current and not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return current or None


def _toplevel(directory):
    current = directory
    while True:
        if os.path.exists(os.path.join(current, ".git")):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def _where_label(path, whole=False):
    """A short, stable label for a place with no usable remote, so edits group sensibly: a
    checkout's own path (`whole`), else the first component under $HOME (`~/.config`) or under
    `/` (`/etc`). Paths under $HOME are written `~/...`."""
    home = os.path.expanduser("~").rstrip("/")
    if path == home:
        return "~"
    if home and path.startswith(home + "/"):
        rest = path[len(home) + 1:]
        return "~/" + (rest if whole else rest.split("/")[0])
    if whole:
        return path
    first = path.strip("/").split("/")[0]
    return "/" + first if first else "/"


_REMOTE_SECTION_RE = re.compile(r'\[remote\s+"origin"\]')


def _origin_from_config(top):
    """`remote.origin.url` read straight from the checkout's config file -- every Edit runs this in
    a fresh process, so a `git` subprocess per edit is the cost avoided. Follows a worktree's
    `.git` file to its gitdir and that gitdir's `commondir`. Returns the URL, None when the config
    has no origin, or False when no config file could be read (the caller then asks git)."""
    try:
        dotgit = os.path.join(top, ".git")
        if os.path.isdir(dotgit):
            gitdir = dotgit
        else:
            with open(dotgit, "r", encoding="utf-8", errors="replace") as fh:
                pointer = fh.read().strip()
            if not pointer.startswith("gitdir:"):
                return False
            gitdir = pointer[len("gitdir:"):].strip()
            if not os.path.isabs(gitdir):
                gitdir = os.path.normpath(os.path.join(top, gitdir))
            common = os.path.join(gitdir, "commondir")
            if os.path.isfile(common):
                with open(common, "r", encoding="utf-8", errors="replace") as fh:
                    target = fh.read().strip()
                gitdir = target if os.path.isabs(target) else os.path.normpath(
                    os.path.join(gitdir, target))
        in_origin = False
        with open(os.path.join(gitdir, "config"), "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if line.startswith("["):
                    in_origin = bool(_REMOTE_SECTION_RE.fullmatch(line))
                    continue
                if in_origin and "=" in line:
                    key, _, value = line.partition("=")
                    if key.strip().lower() == "url":
                        return value.strip().strip('"') or None
        return None
    except Exception:
        return False


def locate(path):
    """`(repo, where)` for a file or directory: `repo` is the enclosing checkout's origin as
    `owner/name`; without one, `where` labels the place instead. Cached per checkout."""
    if not isinstance(path, str) or not path:
        return None, None
    directory = _existing_dir(path)
    if directory is None:
        return None, None
    top = _toplevel(directory)
    if top is None:
        return None, _where_label(directory)
    if top not in _LOCATE_CACHE:
        url = _origin_from_config(top)
        if url is False:  # no readable config file where one should be: ask git itself
            url = _git(["-C", top, "config", "--get", "remote.origin.url"])
        repo = normalize_remote(url)
        _LOCATE_CACHE[top] = (repo, None if repo else _where_label(top, whole=True))
    return _LOCATE_CACHE[top]


def head_commit(directory):
    """`(short_sha, subject)` of HEAD in `directory`, or (None, None)."""
    out = _git(["-C", directory, "log", "-1", "--format=%h%x1f%s"]) if directory else None
    if not out or "\x1f" not in out:
        return None, None
    sha, subject = out.split("\x1f", 1)
    return sha.strip() or None, subject.strip() or None


def current_branch(directory):
    return _git(["-C", directory, "branch", "--show-current"]) if directory else None


def parse_pr_url(url):
    """`(repo, number)` from a github.com pull request URL, or (None, None)."""
    match = _PR_URL_RE.search(url) if isinstance(url, str) else None
    if not match:
        return None, None
    return "%s/%s" % (match.group(1), match.group(2)), int(match.group(3))


# --- state ---------------------------------------------------------------------------------------


def mutate_session(sid, fn):
    """Run fn(session) inside ONE locked read-modify-write of the state file and return its
    result; None when there is no session id or anything fails. Never hold this lock across a
    network call: it serializes every hook of every session on the machine."""
    if not isinstance(sid, str) or not sid:
        return None
    path = state_mod.state_path()

    def mutate():
        state = state_mod.load(path)
        sessions = state.setdefault("sessions", {})
        session = sessions.get(sid)
        if not isinstance(session, dict):
            session = {}
            sessions[sid] = session
        state_mod.ensure_session_shape(session)
        result = fn(session)
        session["updatedAt"] = state_mod.now_iso()
        state_mod.save(path, state_mod.finalize(state))
        return result

    try:
        return state_mod.with_lock(path, mutate)
    except Exception:
        return None


def read_session(sid):
    """A point-in-time copy of one session (no lock: for decisions that precede a network call)."""
    if not isinstance(sid, str) or not sid:
        return {}
    try:
        session = state_mod.load().get("sessions", {}).get(sid)
    except Exception:
        return {}
    return session if isinstance(session, dict) else {}


# --- CLI -----------------------------------------------------------------------------------------


def _cmd_edit(args):
    if not args:
        return
    sid = args[0]
    repo, where = locate(args[1]) if len(args) > 1 and args[1] else (None, None)
    now = utcnow()

    def apply(session):
        count = session.get("edits")
        session["edits"] = (count if _is_count(count) else 0) + 1  # the pre-0.33.0 counter
        record_edit(session, now, repo=repo, where=where)

    mutate_session(sid, apply)


def _cmd_watch(args):
    if len(args) < 2:
        return
    repo, number = parse_pr_url(args[1])
    if repo is None:
        return
    why = args[2] if len(args) > 2 and args[2] else "branch"
    now = utcnow()
    mutate_session(args[0], lambda session: add_watch(session, now, repo, number=number, why=why))


def _cmd_brief(args):
    if not args:
        return
    now = utcnow()
    mutate_session(args[0], lambda session: mark_brief(session, now))


_COMMANDS = {"edit": _cmd_edit, "watch": _cmd_watch, "brief": _cmd_brief}


def main(argv):
    if argv and argv[0] in _COMMANDS:
        _COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit
    sys.exit(0)
