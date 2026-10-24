#!/usr/bin/env python3
"""Unit tests for publish_reminder.py, the gitian-kb nudge layer's Stop-hook completion checkpoint.

Drives it end to end via `sh publish-reminder.sh` (matching how hooks.json actually invokes it),
with GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test. State is seeded by writing the JSON
file with timestamps relative to the REAL now (the hook reads the real clock), and the pull request
poll is answered by a fake `gh` placed first on the hook's PATH -- a double is installed in every
test, failing by default, so nothing here can ever reach GitHub or a real ~/.claude/gitian-kb.

The decision itself (ledger.checkpoint_due) has its own unit tests in test_ledger.py; what is
pinned here is how the HOOK reaches it: the guards, what is polled and when, what is written, what
is printed, and that the state lock is never held across `gh`.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_publish_reminder.py
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOKS_DIR))

import ledger  # noqa: E402  (the modules under test live one directory up)
import pr_poll  # noqa: E402

PUBLISH_REMINDER_SH = HOOKS_DIR / "publish-reminder.sh"
PUBLISH_REMINDER_PY = HOOKS_DIR / "publish_reminder.py"
# Absolute, so a test may hand the hook a PATH that holds no `sh` at all.
SH = shutil.which("sh") or "/bin/sh"

SID = "sess-1"
OTHER_SID = "sess-2"
API = "acme/api"
WEB = "acme/web"

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


# --- fixtures: every timestamp is relative to the real now --------------------------------------


def stamp(minutes_ago=0.0):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime(TIME_FORMAT)


def seconds_ago(value):
    parsed = datetime.strptime(value, TIME_FORMAT).replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - parsed).total_seconds()


def commit(ago, repo=API, ref="abc1234", text="Fix the thing"):
    return {"at": stamp(ago), "kind": "commit", "repo": repo, "ref": ref, "text": text}


def merge(ago, repo=API, number=650, text="Ship the thing"):
    return {"at": stamp(ago), "kind": "merge", "repo": repo, "ref": "#%d" % number, "text": text}


def release(ago, repo=API, tag="v1.2.0", draft=False):
    event = {"at": stamp(ago), "kind": "release", "repo": repo, "ref": tag}
    if draft:
        event["draft"] = True
    return event


def opened(ago, repo=API, number=651, text="Next step"):
    return {"at": stamp(ago), "kind": "pr", "repo": repo, "ref": "#%d" % number, "text": text}


def ruling(ago, text="never merge with --auto on gitian"):
    return {"at": stamp(ago), "kind": "ruling", "text": text}


def edits(ago, n, repo=API):
    at = stamp(ago)
    return {"min": at[:16], "at": at, "repo": repo, "n": n}


MERGED_AT = stamp(5)  # a merge five minutes ago; watches default to being added 120 minutes ago


def watching(number=650, repo=API, branch=None, why="merge", added_ago=120, polled_ago=None):
    entry = {"repo": repo, "why": why, "addedAt": stamp(added_ago)}
    if branch is not None:
        entry["branch"] = branch
    else:
        entry["number"] = number
    if polled_ago is not None:
        entry["polledAt"] = stamp(polled_ago)
    return entry


def session(
    events=(),
    edit_buckets=(),
    watch=(),
    nudged_ago=None,
    poll_ago=None,
    checkpoint_ago=None,
    brief_ago=None,
    **extra
):
    """One session as state.json holds it, with only the fields a test names."""
    led = {"events": list(events), "edits": list(edit_buckets)}
    if checkpoint_ago is not None:
        led["checkpointAt"] = stamp(checkpoint_ago)
    if brief_ago is not None:
        led["briefAt"] = stamp(brief_ago)
    nudges = {}
    if nudged_ago is not None:
        nudges["at"] = stamp(nudged_ago)
    if poll_ago is not None:
        nudges["pollAt"] = stamp(poll_ago)
    doc = {"ledger": led, "watch": list(watch), "nudges": nudges, "updatedAt": stamp()}
    doc.update(extra)
    return doc


def pull_request(number, state="OPEN", title=None):
    """One pull request as GitHub's GraphQL answers it."""
    return {
        "number": number,
        "state": state,
        "title": "PR %d" % number if title is None else title,
        # Relative to now, like every other stamp here: a fixed date would turn stale under the
        # rule that a merge from long before its watch began is a reused branch, not news.
        "mergedAt": MERGED_AT if state == "MERGED" else None,
    }


# pr_poll's documented aliases: repository `r0`, a pull request number `p<N>`, a branch `b0`.
MERGED_650 = {"r0": {"p650": pull_request(650, "MERGED", "Ship the thing")}}
CLOSED_650 = {"r0": {"p650": pull_request(650, "CLOSED", "Abandoned")}}
OPEN_650 = {"r0": {"p650": pull_request(650, "OPEN", "Still going")}}
MERGED_BRANCH = {"r0": {"b0": {"nodes": [pull_request(651, "MERGED", "Next step")]}}}

# A test double for `gh`: counts its calls, keeps the query it was handed, can wait for a second
# caller (so two hooks overlap deterministically), can report whether the state lock was free while
# it ran, then answers with the canned response.
FAKE_GH = r"""#!/bin/sh
printf 'call\n' >> "$GKS_FAKE_GH_CALLS"
printf '%s' "$4" > "$GKS_FAKE_GH_QUERY"
if [ -n "${GKS_FAKE_GH_BARRIER-}" ]; then
  : > "$GKS_FAKE_GH_BARRIER.$$"
  tries=0
  while [ "$(ls "$GKS_FAKE_GH_BARRIER".* 2>/dev/null | wc -l)" -lt 2 ] && [ "$tries" -lt 200 ]; do
    sleep 0.05
    tries=$((tries + 1))
  done
fi
if [ -n "${GKS_FAKE_GH_LOCK-}" ]; then
  python3 -c 'import fcntl, sys
fh = open(sys.argv[1], "a+")
try:
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("free")
except OSError:
    print("held")' "$GKS_FAKE_GH_LOCK" >> "$GKS_FAKE_GH_LOCKLOG"
fi
cat "$GKS_FAKE_GH_RESPONSE"
exit "${GKS_FAKE_GH_EXIT-0}"
"""


class PublishReminderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-publish-reminder-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.bin_dir = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.bin_dir)
        self.gh_calls_file = os.path.join(self.tmpdir, "gh-calls")
        self.gh_query_file = os.path.join(self.tmpdir, "gh-query")
        self.gh_response_file = os.path.join(self.tmpdir, "gh-response.json")
        self.gh_lock_log = os.path.join(self.tmpdir, "gh-lock-log")
        self.base_path = os.environ.get("PATH", "")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env.pop("GITIAN_KB_URL", None)
        # Never let a test reach the real GitHub: unless a test says otherwise, `gh` fails.
        self.install_gh(data=None, exit_code=1)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- the fake gh ------------------------------------------------------------------------

    def install_gh(self, data, exit_code=0, barrier=False, probe_lock=False):
        path = os.path.join(self.bin_dir, "gh")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(FAKE_GH)
        os.chmod(path, 0o755)
        with open(self.gh_response_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"data": data}))
        self.env["PATH"] = self.bin_dir + os.pathsep + self.base_path
        self.env["GKS_FAKE_GH_CALLS"] = self.gh_calls_file
        self.env["GKS_FAKE_GH_QUERY"] = self.gh_query_file
        self.env["GKS_FAKE_GH_RESPONSE"] = self.gh_response_file
        self.env["GKS_FAKE_GH_EXIT"] = str(exit_code)
        self.env["GKS_FAKE_GH_LOCKLOG"] = self.gh_lock_log
        for name in ("GKS_FAKE_GH_BARRIER", "GKS_FAKE_GH_LOCK"):
            self.env.pop(name, None)
        if barrier:
            self.env["GKS_FAKE_GH_BARRIER"] = os.path.join(self.tmpdir, "barrier")
        if probe_lock:
            self.env["GKS_FAKE_GH_LOCK"] = self.state_file + ".lock"

    def gh_calls(self):
        try:
            with open(self.gh_calls_file, encoding="utf-8") as fh:
                return len(fh.read().splitlines())
        except FileNotFoundError:
            return 0

    def gh_query(self):
        with open(self.gh_query_file, encoding="utf-8") as fh:
            return fh.read()

    def shim_dir_without_gh(self):
        """A PATH directory holding only what publish-reminder.sh itself needs (python3 and
        dirname), so `gh` is genuinely absent however this machine is set up."""
        shim_dir = os.path.join(self.tmpdir, "no-gh-bin")
        os.makedirs(shim_dir, exist_ok=True)
        for name, real in (("python3", sys.executable), ("dirname", shutil.which("dirname"))):
            if not real:
                self.skipTest("no %s to shim" % name)
            shim = os.path.join(shim_dir, name)
            with open(shim, "w", encoding="utf-8") as fh:
                fh.write('#!/bin/sh\nexec "%s" "$@"\n' % real)
            os.chmod(shim, 0o755)
        self.assertIsNone(shutil.which("gh", path=shim_dir))
        return shim_dir

    # --- state ------------------------------------------------------------------------------

    def seed(self, session_doc, sid=SID, others=None):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        sessions = {sid: session_doc}
        sessions.update(others or {})
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump({"schemaVersion": 1, "servers": {}, "sessions": sessions}, fh)

    def state_bytes(self):
        try:
            with open(self.state_file, "rb") as fh:
                return fh.read()
        except (FileNotFoundError, IsADirectoryError):
            return None

    def session_state(self, sid=SID):
        return json.loads(self.state_bytes())["sessions"][sid]

    def edit_session(self, change, sid=SID):
        """Rewrite one seeded session in place (e.g. to let time 'pass' for the nudge stamp)."""
        doc = json.loads(self.state_bytes())
        change(doc["sessions"][sid])
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    # --- envelope / invocation --------------------------------------------------------------

    def envelope(self, session_id=SID, stop_hook_active=False, transcript_path=None, **extra):
        payload = {
            "session_id": session_id,
            "transcript_path": transcript_path or os.path.join(self.tmpdir, "transcript.jsonl"),
            "cwd": "/repo",
            "hook_event_name": "Stop",
            "stop_hook_active": stop_hook_active,
        }
        payload.update(extra)
        return payload

    def run_hook(self, payload, env=None):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [SH, str(PUBLISH_REMINDER_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=env or self.env,
            timeout=30,
        )

    # --- assertions -------------------------------------------------------------------------

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertEqual(proc.stdout, "")

    def assert_block(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload.get("decision"), "block")
        self.assertTrue(payload.get("reason"))
        return payload

    def assert_just_now(self, value):
        self.assertIsInstance(value, str)
        self.assertLess(abs(seconds_ago(value)), 30)


class CheckpointDecision(PublishReminderTestCase):
    """Every branch of ledger.checkpoint_due as the Stop hook reaches it (never at a prompt): only
    a completion point blocks, because a block is a whole extra turn."""

    def test_the_first_check_of_a_session_with_open_work_fires(self):
        self.seed(session(events=[commit(5)]))
        self.assert_block(self.run_hook(self.envelope()))

    def test_edits_fire_at_three_and_not_at_two(self):
        self.seed(session(edit_buckets=[edits(5, 2)]))
        self.assert_silent(self.run_hook(self.envelope()))
        self.seed(session(edit_buckets=[edits(5, 3)]))
        self.assert_block(self.run_hook(self.envelope()))

    def test_edits_in_different_buckets_and_repos_add_up(self):
        self.seed(session(edit_buckets=[edits(9, 2, repo=API), edits(4, 1, repo=WEB)]))
        self.assert_block(self.run_hook(self.envelope()))

    def test_a_non_draft_release_fires_and_a_draft_does_not(self):
        self.seed(session(events=[release(5, draft=True)]))
        self.assert_silent(self.run_hook(self.envelope()))
        self.seed(session(events=[release(5)]))
        self.assert_block(self.run_hook(self.envelope()))

    def test_info_events_never_fire(self):
        # An opened pull request and a drafted release are information: the first check fires for
        # none of them, and neither does any amount of them.
        self.seed(session(events=[opened(5), opened(4, number=652), release(3, draft=True)]))
        self.assert_silent(self.run_hook(self.envelope()))

    def test_a_ruling_alone_never_blocks(self):
        # A standing rule gets a hint on the prompt that carried it; a false positive of a text
        # heuristic must never cost a turn.
        self.seed(session(events=[ruling(5), ruling(4, "always rebase before you push")]))
        self.assert_silent(self.run_hook(self.envelope()))
        self.seed(session(events=[ruling(5)], edit_buckets=[edits(4, 2)]))
        self.assert_silent(self.run_hook(self.envelope()))

    def test_work_after_a_nudge_never_blocks_however_long_ago(self):
        # Commits and edits re-arm at the next PROMPT (context, no turn), never here.
        for nudged in (10, 180, 1500):
            with self.subTest(nudged_minutes_ago=nudged):
                self.seed(
                    session(events=[commit(1), commit(2, ref="def5678")],
                            edit_buckets=[edits(1, 5)], nudged_ago=nudged)
                )
                before = self.state_bytes()
                self.assert_silent(self.run_hook(self.envelope()))
                self.assertEqual(self.state_bytes(), before)

    def test_a_merge_inside_the_completion_floor_waits(self):
        self.seed(session(events=[merge(1)], nudged_ago=10))
        self.assert_silent(self.run_hook(self.envelope()))

    def test_a_merge_past_the_completion_floor_fires(self):
        self.seed(session(events=[merge(1)], nudged_ago=45))
        self.assert_block(self.run_hook(self.envelope()))

    def test_a_release_past_the_completion_floor_fires(self):
        self.seed(session(events=[release(1)], nudged_ago=45))
        self.assert_block(self.run_hook(self.envelope()))

    def test_the_floor_runs_from_the_latest_nudge_brief_or_covering_write(self):
        waits = (
            {"nudged_ago": 10},  # shown 10 minutes ago
            {"nudged_ago": 180, "brief_ago": 20},  # a scribe was briefed 20 minutes ago
            {"nudged_ago": 180, "checkpoint_ago": 20},  # a KB write landed 20 minutes ago
        )
        fires = (
            {"nudged_ago": 45},
            {"nudged_ago": 180, "brief_ago": 45},  # past BRIEF_GRACE, so not a cover either
            {"nudged_ago": 180, "checkpoint_ago": 45},
        )
        for kwargs in waits:
            with self.subTest(waits=kwargs):
                self.seed(session(events=[merge(1)], **kwargs))
                self.assert_silent(self.run_hook(self.envelope()))
        for kwargs in fires:
            with self.subTest(fires=kwargs):
                self.seed(session(events=[merge(1)], **kwargs))
                self.assert_block(self.run_hook(self.envelope()))

    def test_a_pending_brief_covers_what_came_before_it(self):
        # The agent briefed kb-scribe five minutes ago: the commit and the merge are the brief's,
        # and a compliant session sees nothing at all.
        self.seed(session(events=[commit(10), merge(8)], brief_ago=5))
        self.assert_silent(self.run_hook(self.envelope()))

    def test_a_brief_whose_scribe_never_published_stops_covering(self):
        # 40 minutes on, the grace is over: the merge it promised to write resurfaces.
        self.seed(session(events=[merge(50)], brief_ago=40))
        payload = self.assert_block(self.run_hook(self.envelope()))
        self.assertIn("#650", payload["reason"])

    def test_a_covering_write_silences_what_it_covers(self):
        self.seed(session(events=[commit(10), merge(8)], checkpoint_ago=5))
        self.assert_silent(self.run_hook(self.envelope()))

    def test_nothing_new_since_the_last_nudge_stays_silent(self):
        # The merge is still open (no write covered it) but the last nudge already showed it.
        self.seed(session(events=[merge(60)], nudged_ago=45))
        self.assert_silent(self.run_hook(self.envelope()))

    def test_an_empty_ledger_is_silent_and_writes_nothing(self):
        self.seed(session())
        before = self.state_bytes()
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.state_bytes(), before)


class BlockShape(PublishReminderTestCase):
    def test_the_block_is_one_json_line_with_a_decision_and_a_reason(self):
        self.seed(session(events=[commit(5)]))
        proc = self.run_hook(self.envelope())
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertTrue(proc.stdout.endswith("\n"))
        self.assertEqual(proc.stdout.count("\n"), 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(sorted(payload), ["decision", "reason"])
        self.assertEqual(payload["decision"], "block")

    def test_the_reason_lists_the_repo_lines_most_recent_first_and_ends_with_the_tail(self):
        # acme/api is listed first in the ledger, but acme/web has the latest activity (1 minute
        # ago against 2), so its line comes first.
        self.seed(
            session(
                events=[commit(20, repo=API), merge(2, repo=API, number=650),
                        commit(1, repo=WEB, ref="def5678", text="Wire the rule")],
                edit_buckets=[edits(3, 4, repo=WEB)],
            )
        )
        reason = self.assert_block(self.run_hook(self.envelope()))["reason"]
        lines = reason.split("\n")
        self.assertTrue(lines[0].startswith("gitian-kb checkpoint: activity since "))
        self.assertTrue(lines[0].endswith("that no KB write covers yet:"))
        repo_lines = [line for line in lines if line.startswith("- ")]
        self.assertEqual([line.split(":")[0] for line in repo_lines], ["- acme/web", "- acme/api"])
        web_line, api_line = repo_lines
        self.assertIn('merged #650 "Ship the thing"', api_line)
        self.assertIn("1 commit", api_line)
        self.assertIn('def5678 "Wire the rule"', web_line)
        self.assertIn("4 file edits", web_line)
        self.assertTrue(reason.endswith(ledger.CHECKPOINT_TAIL))
        self.assertIn("kb-scribe", reason)

    def test_the_reason_names_a_standing_rule_the_user_gave(self):
        self.seed(session(events=[commit(5), ruling(4, "never merge with --auto on gitian")]))
        reason = self.assert_block(self.run_hook(self.envelope()))["reason"]
        self.assertIn('- the user: "never merge with --auto on gitian"', reason)

    def test_the_nudge_is_stamped_and_a_second_stop_is_silent(self):
        self.seed(session(events=[commit(5)]))
        self.assertNotIn("at", self.session_state()["nudges"])
        self.assert_block(self.run_hook(self.envelope()))
        self.assert_just_now(self.session_state()["nudges"]["at"])

        after_first = self.state_bytes()
        self.assert_silent(self.run_hook(self.envelope()))
        # Nothing new, so nothing is written either.
        self.assertEqual(self.state_bytes(), after_first)

    def test_a_new_completion_re_arms_after_the_floor_and_lists_the_old_work_too(self):
        self.seed(session(events=[commit(5)]))
        self.assert_block(self.run_hook(self.envelope()))

        def later(sess):
            # Time passes: the first nudge was 45 minutes ago, and a merge has landed since.
            sess["nudges"]["at"] = stamp(45)
            sess["ledger"]["events"].append(merge(1))

        self.edit_session(later)
        reason = self.assert_block(self.run_hook(self.envelope()))["reason"]
        self.assertIn("merged #650", reason)
        self.assertIn("1 commit", reason)
        self.assert_just_now(self.session_state()["nudges"]["at"])

    def test_the_transcript_path_is_remembered_for_a_later_clear(self):
        transcript = os.path.join(self.tmpdir, "t.jsonl")
        self.seed(session(events=[commit(5)]))
        self.assert_block(self.run_hook(self.envelope(transcript_path=transcript)))
        self.assertEqual(self.session_state()["ledger"]["transcript"], transcript)

    def test_the_state_the_block_leaves_is_what_the_ledger_expects(self):
        self.seed(session(events=[commit(5)]))
        self.assert_block(self.run_hook(self.envelope()))
        led = self.session_state()["ledger"]
        self.assertEqual([e["kind"] for e in led["events"]], ["commit"])  # shown, not pruned
        self.assertNotIn("checkpointAt", led)  # only a KB write moves the checkpoint


class PollingTheWatch(PublishReminderTestCase):
    """The hook is the only place a merge is ever learned: it polls the due watch entries through a
    fake `gh`, folds the answer and the nudge into ONE locked write, and keeps the lock out of the
    network call."""

    def test_a_merged_pull_request_blocks_naming_it_and_leaves_the_watch(self):
        self.seed(session(watch=[watching(650)]))
        self.install_gh(MERGED_650)
        payload = self.assert_block(self.run_hook(self.envelope()))
        self.assertIn('merged #650 "Ship the thing"', payload["reason"])
        self.assertIn("- acme/api:", payload["reason"])

        self.assertEqual(self.gh_calls(), 1)
        state = self.session_state()
        self.assertEqual(state["watch"], [])
        merges = [e for e in state["ledger"]["events"] if e["kind"] == "merge"]
        self.assertEqual(len(merges), 1)
        self.assertEqual((merges[0]["repo"], merges[0]["ref"]), (API, "#650"))
        # The session merged it itself (why "merge"): stamped with GitHub's mergedAt, so a brief
        # written right after the merge covers it (ledger._merge_stamp).
        self.assertEqual(merges[0]["at"], MERGED_AT)
        self.assert_just_now(state["nudges"]["pollAt"])
        self.assert_just_now(state["nudges"]["at"])

    def test_an_armed_merge_is_stamped_when_the_poll_saw_it(self):
        self.seed(session(watch=[watching(650, why="armed")]))
        self.install_gh(MERGED_650)
        self.assert_block(self.run_hook(self.envelope()))
        merges = [e for e in self.session_state()["ledger"]["events"] if e["kind"] == "merge"]
        self.assertEqual(len(merges), 1)
        self.assert_just_now(merges[0]["at"])

    def test_a_merged_branch_watch_blocks_naming_the_pull_request_it_found(self):
        self.seed(session(watch=[watching(branch="feat/x", why="branch")]))
        self.install_gh(MERGED_BRANCH)
        payload = self.assert_block(self.run_hook(self.envelope()))
        self.assertIn("merged #651", payload["reason"])
        # SessionStart's branch PR may be someone else's: its title stays out of the nudge.
        self.assertNotIn("Next step", payload["reason"])
        self.assertEqual(self.session_state()["watch"], [])

    def test_gh_is_handed_one_query_for_every_due_entry(self):
        self.seed(session(watch=[watching(650), watching(branch="feat/x"), watching(7, repo=WEB)]))
        self.install_gh({})
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.gh_calls(), 1)
        query = self.gh_query()
        self.assertTrue(query.startswith("query="))
        for needle in ("p650", '"feat/x"', 'name: "web"', "p7"):
            self.assertIn(needle, query)

    def test_a_closed_pull_request_is_dropped_without_a_merge_or_a_block(self):
        self.seed(session(watch=[watching(650)]))
        self.install_gh(CLOSED_650)
        self.assert_silent(self.run_hook(self.envelope()))
        state = self.session_state()
        self.assertEqual(state["watch"], [])
        self.assertEqual(state["ledger"]["events"], [])
        self.assert_just_now(state["nudges"]["pollAt"])

    def test_an_open_pull_request_stays_watched_and_is_stamped(self):
        self.seed(session(watch=[watching(650)]))
        self.install_gh(OPEN_650)
        self.assert_silent(self.run_hook(self.envelope()))
        state = self.session_state()
        self.assertEqual(len(state["watch"]), 1)
        self.assert_just_now(state["watch"][0]["polledAt"])
        self.assert_just_now(state["nudges"]["pollAt"])
        self.assertNotIn("at", state["nudges"])  # nothing was shown

    def test_the_poll_waits_out_its_interval(self):
        self.seed(session(watch=[watching(650)]))
        self.install_gh(OPEN_650)
        self.assert_silent(self.run_hook(self.envelope()))
        self.assert_silent(self.run_hook(self.envelope()))
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.gh_calls(), 1)

    def test_an_entry_that_was_never_polled_polls_again_inside_the_interval(self):
        self.seed(
            session(watch=[watching(650, polled_ago=1), watching(7, repo=WEB)], poll_ago=1)
        )
        self.install_gh(OPEN_650)
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.gh_calls(), 1)
        self.assertIn("p7", self.gh_query())

    def test_the_poll_runs_again_once_the_interval_has_passed(self):
        self.seed(session(watch=[watching(650, polled_ago=10)], poll_ago=10))
        self.install_gh(OPEN_650)
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.gh_calls(), 1)

    def test_a_recently_polled_watch_costs_no_process_at_all(self):
        self.seed(session(watch=[watching(650, polled_ago=1)], poll_ago=1))
        before = self.state_bytes()
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.gh_calls(), 0)
        self.assertEqual(self.state_bytes(), before)

    def test_a_gh_that_fails_is_stamped_and_retried_on_the_interval_not_every_turn(self):
        self.seed(session(watch=[watching(650)]))
        self.install_gh(None, exit_code=1)
        self.assert_silent(self.run_hook(self.envelope()))
        state = self.session_state()
        self.assertEqual(len(state["watch"]), 1)  # nothing was learned, so it is kept
        self.assert_just_now(state["watch"][0]["polledAt"])
        self.assert_just_now(state["nudges"]["pollAt"])

        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.gh_calls(), 1)

    def test_a_missing_gh_is_no_crash_and_still_stamps_the_poll(self):
        self.seed(session(watch=[watching(650)], events=[commit(5)]))
        env = dict(self.env, PATH=self.shim_dir_without_gh())
        # The open commit is what blocks; the missing gh must not stop that from being said.
        self.assert_block(self.run_hook(self.envelope(), env=env))
        state = self.session_state()
        self.assertEqual(len(state["watch"]), 1)
        self.assert_just_now(state["watch"][0]["polledAt"])
        self.assert_just_now(state["nudges"]["pollAt"])
        self.assertEqual(self.gh_calls(), 0)

    def test_a_missing_gh_with_nothing_to_say_is_silent(self):
        self.seed(session(watch=[watching(650)]))
        env = dict(self.env, PATH=self.shim_dir_without_gh())
        self.assert_silent(self.run_hook(self.envelope(), env=env))
        self.assert_just_now(self.session_state()["nudges"]["pollAt"])

    def test_a_merge_inside_the_floor_is_recorded_and_waits_for_the_floor(self):
        self.seed(session(watch=[watching(650)], nudged_ago=10))
        self.install_gh(MERGED_650)
        self.assert_silent(self.run_hook(self.envelope()))
        state = self.session_state()
        self.assertEqual(state["watch"], [])
        self.assertEqual([e["kind"] for e in state["ledger"]["events"]], ["merge"])

        def floor_passes(sess):
            sess["nudges"]["at"] = stamp(45)

        # The floor passes, and the merge the poll recorded is named without asking GitHub again.
        self.edit_session(floor_passes)
        payload = self.assert_block(self.run_hook(self.envelope()))
        self.assertIn("merged #650", payload["reason"])
        self.assertEqual(self.gh_calls(), 1)

    def test_a_merge_found_by_the_poll_is_listed_with_the_work_already_open(self):
        self.seed(session(watch=[watching(650)], events=[commit(5, repo=WEB)]))
        self.install_gh(MERGED_650)
        reason = self.assert_block(self.run_hook(self.envelope()))["reason"]
        self.assertIn("- acme/api: merged #650", reason)
        self.assertIn("- acme/web: 1 commit", reason)

    def test_hostile_or_corrupt_watch_entries_never_reach_gh(self):
        hostile = {
            "repo": API,
            "number": "1) { viewer { login } } #",
            "why": "merge",
            "addedAt": stamp(60),
        }
        branchy = {"repo": API, "branch": 'x" }) { id } #', "why": "branch", "addedAt": stamp(60)}
        self.seed(session(watch=[hostile, branchy, "junk", {"repo": 3}, watching(650)]))
        self.install_gh(MERGED_650)
        self.assert_block(self.run_hook(self.envelope()))
        query = self.gh_query()
        self.assertIn("p650", query)
        for poison in ("viewer", "{ id }", "junk"):
            self.assertNotIn(poison, query)

    def test_an_expired_watch_entry_is_not_polled(self):
        self.seed(session(watch=[watching(650, added_ago=73 * 60)]))
        self.install_gh(MERGED_650)
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.gh_calls(), 0)

    def test_the_state_lock_is_not_held_while_gh_runs(self):
        # The lock serializes every hook of every session on the machine; the fake gh tries to take
        # it from its own process while the hook is waiting on it.
        self.seed(session(watch=[watching(650)], events=[commit(5)]))
        self.install_gh(MERGED_650, probe_lock=True)
        self.assert_block(self.run_hook(self.envelope()))
        with open(self.gh_lock_log, encoding="utf-8") as fh:
            self.assertEqual(fh.read().split(), ["free"])

    def test_two_stops_racing_for_one_completion_block_once(self):
        # The fake gh holds each hook until both have called it, and the call comes after the
        # snapshot and before the write: so both read the snapshot before either writes, both
        # decide to fire, and the second must find the first's nudge when it re-decides under the
        # lock.
        self.seed(session(watch=[watching(650)], events=[commit(5)]))
        self.install_gh(OPEN_650, barrier=True)
        payload = json.dumps(self.envelope())
        procs = [
            subprocess.Popen(
                [SH, str(PUBLISH_REMINDER_SH)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
            )
            for _ in range(2)
        ]
        for proc in procs:
            proc.stdin.write(payload)
            proc.stdin.close()
        outputs = []
        for proc in procs:
            outputs.append(proc.stdout.read())
            proc.stdout.close()
            proc.stderr.close()
            self.assertEqual(proc.wait(timeout=30), 0)
        self.assertEqual(self.gh_calls(), 2)  # the race really happened
        blocks = [out for out in outputs if out]
        self.assertEqual(len(blocks), 1, msg="outputs=%r" % (outputs,))
        self.assertEqual(json.loads(blocks[0])["decision"], "block")


class Guards(PublishReminderTestCase):
    """Silent -- no poll, no write -- for a loop, a subagent, no session; the fake gh answers MERGED
    and the seeded state would fire, so any guard that failed would show as a call or a block."""

    def arm(self):
        self.seed(session(watch=[watching(650)], events=[commit(5)]))
        self.install_gh(MERGED_650)
        return self.state_bytes()

    def test_stop_hook_active_is_silent_polls_nothing_and_writes_nothing(self):
        for active in (True, "true", 1):
            with self.subTest(stop_hook_active=active):
                before = self.arm()
                self.assert_silent(self.run_hook(self.envelope(stop_hook_active=active)))
                self.assertEqual(self.gh_calls(), 0)
                self.assertEqual(self.state_bytes(), before)

    def test_a_delegated_session_is_silent_polls_nothing_and_writes_nothing(self):
        before = self.arm()
        self.assert_silent(self.run_hook(self.envelope(agent_id="agent-123")))
        self.assertEqual(self.gh_calls(), 0)
        self.assertEqual(self.state_bytes(), before)
        # ... and the nudge is still the primary's to receive.
        self.assert_block(self.run_hook(self.envelope()))

    def test_no_usable_session_id_is_silent(self):
        before = self.arm()
        for sid in (None, "", 5, ["sess-1"]):
            with self.subTest(session_id=sid):
                payload = self.envelope()
                payload["session_id"] = sid
                self.assert_silent(self.run_hook(payload))
        payload = self.envelope()
        del payload["session_id"]
        self.assert_silent(self.run_hook(payload))
        self.assertEqual(self.gh_calls(), 0)
        self.assertEqual(self.state_bytes(), before)

    def test_garbage_stdin_is_silent(self):
        before = self.arm()
        for text in ("", "   ", "{not valid json ][ at all", "[]", "null", '"stop"', "42"):
            with self.subTest(stdin=text):
                self.assert_silent(self.run_hook(text))
        self.assertEqual(self.gh_calls(), 0)
        self.assertEqual(self.state_bytes(), before)

    def test_an_unknown_session_is_silent_and_creates_no_state(self):
        self.assert_silent(self.run_hook(self.envelope(session_id="never-seen")))
        self.assertIsNone(self.state_bytes())
        self.assertEqual(self.gh_calls(), 0)

    def test_it_only_ever_looks_at_its_own_session(self):
        busy = session(watch=[watching(650)], events=[commit(5)])
        self.seed(session(), others={OTHER_SID: busy})
        self.install_gh(MERGED_650)
        before = self.state_bytes()
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.gh_calls(), 0)
        self.assertEqual(self.state_bytes(), before)
        self.assert_block(self.run_hook(self.envelope(session_id=OTHER_SID)))


class RetiredLatches(PublishReminderTestCase):
    """Until 0.33.0 the reminder fired once per epoch and went silent for good after the session's
    first publish, read from a scan of the whole transcript. None of that survives."""

    def test_the_old_once_flag_and_publish_count_no_longer_silence_anything(self):
        self.seed(
            session(events=[commit(5)], flags={"publish_reminder": True}, publishes=4)
        )
        self.assert_block(self.run_hook(self.envelope()))

    def test_a_vocabulary_only_publish_does_not_cover_the_work(self):
        # `publishes` counts publish_topic too; only a content write moves the checkpoint.
        self.seed(session(events=[merge(5)], publishes=1))
        self.assert_block(self.run_hook(self.envelope()))

    def test_the_transcript_is_never_read(self):
        # A transcript full of edits and commits, an empty ledger: the old scan would have fired.
        transcript = os.path.join(self.tmpdir, "busy.jsonl")
        with open(transcript, "w", encoding="utf-8") as fh:
            for _ in range(5):
                tool_use = {"type": "tool_use", "name": "Edit", "input": {"file_path": "/x"}}
                fh.write(json.dumps({"type": "assistant", "message": {"content": [tool_use]}}))
                fh.write("\n")
        self.seed(session())
        self.assert_silent(self.run_hook(self.envelope(transcript_path=transcript)))

        # ... and a transcript that does not exist, or is not a path, costs the ledger nothing.
        self.seed(session(events=[commit(5)]))
        self.assert_block(
            self.run_hook(self.envelope(transcript_path=os.path.join(self.tmpdir, "nope.jsonl")))
        )
        self.seed(session(events=[commit(5)]))
        self.assert_block(self.run_hook(self.envelope(transcript_path=12345)))

    def test_an_epoch_bump_alone_re_arms_nothing(self):
        self.seed(session(events=[commit(5)]))
        self.assert_block(self.run_hook(self.envelope()))
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "state.py"), "bump-epoch", SID],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        self.assert_silent(self.run_hook(self.envelope()))

    def test_it_imports_only_the_ledger_and_the_poll(self):
        # No transcript scan, so no commit_nudge or kb_tool: the module must not need a hook that
        # another change retires.
        tree = ast.parse(PUBLISH_REMINDER_PY.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(imported, {"copy", "json", "sys", "ledger", "pr_poll"})


class Robustness(PublishReminderTestCase):
    def test_a_corrupt_state_file_is_silent(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")
        self.assert_silent(self.run_hook(self.envelope()))

    def test_a_state_path_that_is_a_directory_is_silent(self):
        os.makedirs(self.state_file, exist_ok=True)
        self.assert_silent(self.run_hook(self.envelope()))

    def test_a_corrupt_session_shape_is_survived(self):
        self.seed("garbage")
        self.assert_silent(self.run_hook(self.envelope()))

    def test_corrupt_ledger_parts_read_as_empty(self):
        self.seed({"ledger": "x", "watch": "y", "nudges": 5})
        self.assert_silent(self.run_hook(self.envelope()))
        self.seed(
            {
                "ledger": {"events": "x", "edits": [1, "two", {"at": "never", "n": 3}]},
                "watch": [None, {"repo": API}],
                "nudges": {"at": "yesterday"},
            }
        )
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.gh_calls(), 0)

    def test_an_event_with_an_unreadable_timestamp_is_ignored_not_fatal(self):
        self.seed(session(events=[{"at": "yesterday", "kind": "commit", "repo": API}, commit(5)]))
        self.assert_block(self.run_hook(self.envelope()))

    def test_a_nudge_that_could_not_be_recorded_is_never_shown(self):
        # The lock file cannot be opened, so the write fails after the decision was made. Showing
        # the block anyway would repeat it at every turn end, since nothing remembered it.
        self.seed(session(events=[commit(5)]))
        os.makedirs(self.state_file + ".lock")
        before = self.state_bytes()
        self.assert_silent(self.run_hook(self.envelope()))
        self.assertEqual(self.state_bytes(), before)


class WatchKeys(unittest.TestCase):
    """pr_poll files its answers under entry_key; the ledger finds them under watch_key. They are
    two spellings of one key, or a merge would be polled for and never found."""

    def test_a_number_entry_has_the_same_key_on_both_sides(self):
        entry = watching(650)
        self.assertEqual(pr_poll.entry_key(entry), "acme/api#650")
        self.assertEqual(ledger.watch_key(entry), pr_poll.entry_key(entry))

    def test_a_branch_entry_has_the_same_key_on_both_sides(self):
        entry = watching(branch="feat/x", why="branch")
        self.assertEqual(pr_poll.entry_key(entry), "acme/api@feat/x")
        self.assertEqual(ledger.watch_key(entry), pr_poll.entry_key(entry))

    def test_entries_the_ledger_builds_agree_with_the_poll(self):
        now = ledger.utcnow()
        sess = {}
        ledger.add_watch(sess, now, API, number=650, why="merge")
        ledger.add_watch(sess, now, WEB, branch="release/1.x", why="branch")
        ledger.add_watch(sess, now, "Acme-Org/web_app.v2", number=7)
        self.assertEqual(len(sess["watch"]), 3)
        for entry in sess["watch"]:
            with self.subTest(entry=entry):
                self.assertEqual(ledger.watch_key(entry), pr_poll.entry_key(entry))

    def test_the_poll_answers_what_the_hook_looks_up(self):
        # A result filed under entry_key is found by apply_poll through watch_key.
        now = ledger.utcnow()
        sess = {}
        ledger.add_watch(sess, now, API, number=650, why="merge")
        entry = sess["watch"][0]
        results = {pr_poll.entry_key(entry): {"state": "MERGED", "number": 650, "title": "Ship"}}
        ledger.apply_poll(sess, now, results, polled=[entry])
        self.assertEqual(sess["watch"], [])
        self.assertEqual([e["kind"] for e in sess["ledger"]["events"]], ["merge"])


class NamedAgentSession(PublishReminderTestCase):
    def test_an_agent_type_payload_is_silent_like_the_other_hooks(self):
        # A `claude --agent <name>` session carries agent_type: it cannot brief kb-scribe either.
        self.seed(session(events=[commit(5)]))
        self.assert_silent(self.run_hook(self.envelope(agent_type="kb-scribe")))


if __name__ == "__main__":
    unittest.main()
