#!/usr/bin/env python3
"""Unit tests for state.py, the gitian-kb nudge layer's state substrate.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_state.py
Every test points GITIAN_KB_STATE_FILE at a fresh tempdir so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other.
"""

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
STATE_PY = HOOKS_DIR / "state.py"
sys.path.insert(0, str(HOOKS_DIR))

import state as state_mod  # noqa: E402  (imported for the in-process shape checks)


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-state-test-")
        # Nested + not-yet-existing, so writes must create the directory themselves.
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_state(self, *args, input_text=None):
        return subprocess.run(
            [sys.executable, str(STATE_PY), *args],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

    def merge(self, doc):
        proc = self.run_state("merge", input_text=json.dumps(doc))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        return proc

    def dump_state(self):
        proc = self.run_state("dump")
        self.assertEqual(proc.returncode, 0)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}


class DeepMergeSemantics(StateTestCase):
    def test_dict_recurses_scalar_and_array_replace(self):
        server_key = "https://x.example/api/mcp"
        self.merge(
            {
                "servers": {
                    server_key: {
                        "vocabRev": 1,
                        "topics": [{"slug": "a", "description": "d", "degree": 1}],
                        "undescribedTopics": ["a"],
                    }
                }
            }
        )
        # Scalar replace: only vocabRev changes, sibling keys (topics, undescribedTopics) survive
        # because the dict recursed rather than being clobbered wholesale.
        self.merge({"servers": {server_key: {"vocabRev": 2}}})
        state = self.dump_state()
        server = state["servers"][server_key]
        self.assertEqual(server["vocabRev"], 2)
        self.assertEqual(server["topics"], [{"slug": "a", "description": "d", "degree": 1}])
        self.assertEqual(server["undescribedTopics"], ["a"])

        # Array replace: topics is replaced wholesale, not appended to.
        self.merge({"servers": {server_key: {"topics": [{"slug": "b", "description": "d2", "degree": 2}]}}})
        state = self.dump_state()
        server = state["servers"][server_key]
        self.assertEqual(server["topics"], [{"slug": "b", "description": "d2", "degree": 2}])
        self.assertEqual(server["vocabRev"], 2)  # untouched sibling still survives


class AtomicWrite(StateTestCase):
    def test_creates_dir_0700_and_file_no_stray_tmp(self):
        self.assertFalse(os.path.exists(self.state_file))
        self.merge({"servers": {}})
        self.assertTrue(os.path.isfile(self.state_file))

        directory = os.path.dirname(self.state_file)
        mode = os.stat(directory).st_mode & 0o777
        self.assertEqual(mode, 0o700)

        leftovers = [
            name
            for name in os.listdir(directory)
            if name not in ("state.json", "state.json.lock")
        ]
        self.assertEqual(leftovers, [])


class CorruptStateFailOpen(StateTestCase):
    def _seed_garbage(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")

    def test_every_subcommand_survives_corrupt_file(self):
        self._seed_garbage()
        calls = [
            (["get", "sessions.nope.flags.x"], None),
            (["status"], None),
            (["dump"], None),
            (["incr", "sid1", "edits"], None),
            (["flag-once", "sid1", "orientation"], None),
            (["bump-epoch", "sid1"], None),
            (["merge"], "{}"),
            (["merge"], "not json either"),
            (["bogus-subcommand"], None),
            ([], None),
        ]
        for args, input_text in calls:
            proc = self.run_state(*args, input_text=input_text)
            self.assertEqual(proc.returncode, 0, msg="args=%r stderr=%r" % (args, proc.stderr))

    def test_get_on_missing_path_is_empty_after_corruption(self):
        self._seed_garbage()
        proc = self.run_state("get", "sessions.totally.missing.path")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_next_merge_produces_valid_v1_after_corruption(self):
        self._seed_garbage()
        self.merge({"servers": {}})
        state = self.dump_state()
        self.assertEqual(state.get("schemaVersion"), 1)
        self.assertIsInstance(state.get("servers"), dict)
        self.assertIsInstance(state.get("sessions"), dict)


class FlagOnce(StateTestCase):
    def test_fires_exactly_once_then_silent(self):
        first = self.run_state("flag-once", "sidA", "orientation")
        self.assertEqual(first.returncode, 0)
        self.assertEqual(first.stdout.strip(), "fire")

        second = self.run_state("flag-once", "sidA", "orientation")
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")

        state = self.dump_state()
        self.assertTrue(state["sessions"]["sidA"]["flags"]["orientation"])

    def test_distinct_flags_independent(self):
        self.run_state("flag-once", "sidA", "orientation")
        second_flag = self.run_state("flag-once", "sidA", "publish-lint")
        self.assertEqual(second_flag.stdout.strip(), "fire")


class BumpEpoch(StateTestCase):
    def test_clears_flags_lint_mint_zeroes_counters_keeps_vocab_rev_rearms_flags(self):
        self.run_state("flag-once", "sidB", "orientation")
        self.run_state("incr", "sidB", "edits", "3")
        self.merge(
            {
                "sessions": {
                    "sidB": {
                        "lastSeenVocabRev": {"https://gitian.dev/api/mcp": {"home": 5}},
                        "lintHashes": ["deadbeef"],
                        "mintPrompted": ["some-slug"],
                    }
                }
            }
        )
        before = self.dump_state()["sessions"]["sidB"]
        self.assertEqual(before["epoch"], 0)
        self.assertEqual(before["edits"], 3)
        self.assertTrue(before["flags"]["orientation"])

        proc = self.run_state("bump-epoch", "sidB")
        self.assertEqual(proc.returncode, 0)

        after = self.dump_state()["sessions"]["sidB"]
        self.assertEqual(after["epoch"], 1)
        self.assertEqual(after["flags"], {})
        self.assertEqual(after["lintHashes"], [])
        self.assertEqual(after["mintPrompted"], [])
        self.assertEqual(after["edits"], 0)
        self.assertEqual(after["gitianReads"], 0)
        self.assertEqual(after["publishes"], 0)
        # preserved across epoch bump -- per-(server, kb), see state.py's _SESSION_DEFAULTS
        self.assertEqual(after["lastSeenVocabRev"], {"https://gitian.dev/api/mcp": {"home": 5}})

        rearmed = self.run_state("flag-once", "sidB", "orientation")
        self.assertEqual(rearmed.stdout.strip(), "fire")

    def test_second_bump_increments_again(self):
        self.run_state("bump-epoch", "sidB2")
        self.run_state("bump-epoch", "sidB2")
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sidB2"]["epoch"], 2)


class Incr(StateTestCase):
    def test_default_delta_is_one(self):
        self.run_state("incr", "sidC", "gitianReads")
        self.run_state("incr", "sidC", "gitianReads")
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sidC"]["gitianReads"], 2)

    def test_explicit_delta(self):
        self.run_state("incr", "sidC", "edits", "5")
        self.run_state("incr", "sidC", "edits", "2")
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sidC"]["edits"], 7)


class SevenDayPruning(StateTestCase):
    def test_old_session_pruned_recent_kept(self):
        old_iso = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        seed = {
            "schemaVersion": 1,
            "servers": {},
            "sessions": {
                "old-sid": {
                    "epoch": 0,
                    "flags": {},
                    "gitianReads": 0,
                    "edits": 0,
                    "publishes": 0,
                    "lastSeenVocabRev": {},
                    "lintHashes": [],
                    "mintPrompted": [],
                    "updatedAt": old_iso,
                },
                "recent-sid": {
                    "epoch": 0,
                    "flags": {},
                    "gitianReads": 0,
                    "edits": 0,
                    "publishes": 0,
                    "lastSeenVocabRev": {},
                    "lintHashes": [],
                    "mintPrompted": [],
                    "updatedAt": recent_iso,
                },
            },
        }
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(seed, fh)

        self.merge({})  # any write path runs finalize() -> prune_sessions()

        state = self.dump_state()
        self.assertNotIn("old-sid", state["sessions"])
        self.assertIn("recent-sid", state["sessions"])


class Status(StateTestCase):
    def test_empty_state_does_not_crash(self):
        proc = self.run_state("status")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("servers: none tracked", proc.stdout)
        self.assertIn("sessions: 0 tracked", proc.stdout)

    def test_populated_state_mentions_server_and_current_session(self):
        fetched_at = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.merge(
            {
                "servers": {
                    "https://gitian.dev/api/mcp": {
                        "vocabRev": 3,
                        "vocabFetchedAt": fetched_at,
                        "topics": [{"slug": "a", "description": "d", "degree": 1}],
                        "undescribedTopics": ["a"],
                        "lastPublishAt": None,
                        "lastAppendAt": None,
                        "lastPublishSlug": None,
                    }
                }
            }
        )
        self.run_state("flag-once", "sidZ", "orientation")

        proc = self.run_state("status")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("gitian.dev", proc.stdout)
        self.assertIn("vocabRev=3", proc.stdout)
        self.assertIn("sidZ", proc.stdout)
        self.assertIn("orientation", proc.stdout)


class GetDotpath(StateTestCase):
    def test_nested_flag_value(self):
        self.run_state("flag-once", "sidD", "orientation")
        proc = self.run_state("get", "sessions.sidD.flags.orientation")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "true")

    def test_missing_path_is_empty(self):
        proc = self.run_state("get", "totally.missing.path")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_no_arg_is_silent(self):
        proc = self.run_state("get")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


def _iso_from_now(**delta):
    """An ISO stamp `delta` from the real now (negative = ago), in the shape now_iso() writes."""
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


# The ledger, watch and nudge bookkeeping that 0.33.0 hangs on each session. ledger.py owns the
# shapes; these tests only pin what state.py promises about carrying them.
SAMPLE_LEDGER = {
    "events": [
        {
            "at": "2026-10-24T11:30:00Z",
            "kind": "merge",
            "repo": "acme/widgets",
            "ref": "#5",
            "text": "Fix it",
        }
    ],
    "edits": [{"min": "2026-10-24T11:40", "at": "2026-10-24T11:40:10Z", "n": 4, "repo": "acme/widgets"}],
    "checkpointAt": "2026-10-24T10:00:00Z",
    "briefAt": "2026-10-24T11:00:00Z",
    "scribeIds": ["a1b2c3"],
    "dropped": 2,
    "transcript": "/home/me/.claude/projects/x/abc.jsonl",
}
SAMPLE_WATCH = [
    {
        "repo": "acme/widgets",
        "number": 12,
        "why": "opened",
        "addedAt": "2026-10-24T11:00:00Z",
        "polledAt": "2026-10-24T11:10:00Z",
    },
    {"repo": "acme/widgets", "branch": "feat/x", "why": "branch", "addedAt": "2026-10-24T11:20:00Z"},
]
SAMPLE_NUDGES = {
    "at": "2026-10-24T11:15:00Z",
    "pollAt": "2026-10-24T11:50:00Z",
    "rulingAt": "2026-10-24T11:45:00Z",
}


class LedgerDefaults(StateTestCase):
    def test_ensure_session_shape_adds_an_empty_ledger_watch_and_nudges(self):
        session = state_mod.ensure_session_shape({})
        self.assertEqual(session["ledger"], {})
        self.assertEqual(session["watch"], [])
        self.assertEqual(session["nudges"], {})

    def test_default_session_carries_them_too(self):
        session = state_mod.default_session()
        self.assertEqual((session["ledger"], session["watch"], session["nudges"]), ({}, [], {}))

    def test_what_is_already_there_is_never_overwritten(self):
        mine = {
            "ledger": copy.deepcopy(SAMPLE_LEDGER),
            "watch": copy.deepcopy(SAMPLE_WATCH),
            "nudges": copy.deepcopy(SAMPLE_NUDGES),
            "edits": 7,
        }
        session = state_mod.ensure_session_shape(copy.deepcopy(mine))
        for key, value in mine.items():
            self.assertEqual(session[key], value, msg=key)
        # A wrong-shaped value is ledger.py's to normalize on read; here it is simply left alone.
        odd = state_mod.ensure_session_shape({"ledger": "garbage", "watch": 5, "nudges": None})
        self.assertEqual((odd["ledger"], odd["watch"], odd["nudges"]), ("garbage", 5, None))

    def test_every_session_gets_its_own_containers(self):
        first = state_mod.ensure_session_shape({})
        first["ledger"]["events"] = [1]
        first["watch"].append("x")
        first["nudges"]["at"] = "x"
        second = state_mod.ensure_session_shape({})
        self.assertEqual((second["ledger"], second["watch"], second["nudges"]), ({}, [], {}))

    def test_a_session_created_through_the_cli_has_them(self):
        self.run_state("incr", "sidN", "edits")
        session = self.dump_state()["sessions"]["sidN"]
        self.assertEqual((session["ledger"], session["watch"], session["nudges"]), ({}, [], {}))
        self.assertEqual(session["edits"], 1)

    def test_a_merge_touching_a_session_fills_them_in(self):
        self.merge({"sessions": {"sidM": {"epoch": 2}}})
        session = self.dump_state()["sessions"]["sidM"]
        self.assertEqual(session["epoch"], 2)
        self.assertEqual((session["ledger"], session["watch"], session["nudges"]), ({}, [], {}))

    def test_a_session_from_an_older_install_gains_them_without_losing_anything(self):
        seed = {
            "schemaVersion": 1,
            "servers": {},
            "sessions": {
                "sidOld": {
                    "epoch": 3,
                    "flags": {"orientation": True},
                    "gitianReads": 2,
                    "edits": 5,
                    "publishes": 1,
                    "lastSeenVocabRev": {"https://gitian.dev/api/mcp": {"home": 5}},
                    "lintHashes": [],
                    "mintPrompted": [],
                    "updatedAt": _iso_from_now(hours=-1),
                }
            },
        }
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(seed, fh)
        self.run_state("incr", "sidOld", "gitianReads")
        session = self.dump_state()["sessions"]["sidOld"]
        self.assertEqual((session["ledger"], session["watch"], session["nudges"]), ({}, [], {}))
        self.assertEqual((session["epoch"], session["edits"], session["gitianReads"]), (3, 5, 3))
        self.assertEqual(session["flags"], {"orientation": True})
        self.assertEqual(session["lastSeenVocabRev"], {"https://gitian.dev/api/mcp": {"home": 5}})


class BumpEpochKeepsTheLedger(StateTestCase):
    def seed(self, sid):
        self.merge(
            {
                "sessions": {
                    sid: {
                        "ledger": copy.deepcopy(SAMPLE_LEDGER),
                        "watch": copy.deepcopy(SAMPLE_WATCH),
                        "nudges": copy.deepcopy(SAMPLE_NUDGES),
                        "lintHashes": ["deadbeef"],
                        "mintPrompted": ["some-slug"],
                        "lastSeenVocabRev": {"https://gitian.dev/api/mcp": {"home": 5}},
                    }
                }
            }
        )
        self.run_state("flag-once", sid, "orientation")
        self.run_state("incr", sid, "edits", "4")
        self.run_state("incr", sid, "gitianReads", "2")
        self.run_state("incr", sid, "publishes")

    def test_the_ledger_watch_and_nudges_survive_while_the_old_counters_reset(self):
        self.seed("sidL")
        before = self.dump_state()["sessions"]["sidL"]
        self.assertEqual((before["edits"], before["gitianReads"], before["publishes"]), (4, 2, 1))

        self.assertEqual(self.run_state("bump-epoch", "sidL").returncode, 0)

        after = self.dump_state()["sessions"]["sidL"]
        self.assertEqual(after["ledger"], SAMPLE_LEDGER)
        self.assertEqual(after["watch"], SAMPLE_WATCH)
        self.assertEqual(after["nudges"], SAMPLE_NUDGES)
        # what a bump has always reset
        self.assertEqual(after["epoch"], before["epoch"] + 1)
        self.assertEqual(after["flags"], {})
        self.assertEqual((after["lintHashes"], after["mintPrompted"]), ([], []))
        self.assertEqual((after["edits"], after["gitianReads"], after["publishes"]), (0, 0, 0))
        self.assertEqual(after["lastSeenVocabRev"], {"https://gitian.dev/api/mcp": {"home": 5}})

    def test_they_survive_every_further_bump_too(self):
        self.seed("sidL2")
        for _ in range(3):
            self.run_state("bump-epoch", "sidL2")
        after = self.dump_state()["sessions"]["sidL2"]
        self.assertEqual(after["epoch"], 3)
        self.assertEqual(
            (after["ledger"], after["watch"], after["nudges"]),
            (SAMPLE_LEDGER, SAMPLE_WATCH, SAMPLE_NUDGES),
        )

    def test_a_bump_touches_only_its_own_session(self):
        self.seed("sidA")
        self.seed("sidB")
        self.run_state("bump-epoch", "sidA")
        other = self.dump_state()["sessions"]["sidB"]
        self.assertEqual(other["epoch"], 0)
        self.assertEqual(other["edits"], 4)
        self.assertEqual(other["ledger"], SAMPLE_LEDGER)

    def test_a_bump_on_a_fresh_session_still_works_and_leaves_the_ledger_empty(self):
        self.run_state("bump-epoch", "sidFresh")
        session = self.dump_state()["sessions"]["sidFresh"]
        self.assertEqual(session["epoch"], 1)
        self.assertEqual((session["ledger"], session["watch"], session["nudges"]), ({}, [], {}))


class StatusLedger(StateTestCase):
    HEADLINE = re.compile(
        r"(?m)^  activity no KB write covers yet \(since (\d\d:\d\dZ|\d{4}-\d\d-\d\d \d\d:\d\dZ)\):$"
    )
    EMPTY_LINES = (
        "  activity no KB write covers yet: none",
        "  watching for a merge: nothing",
        "  last KB write covering activity: never; last scribe brief: never; "
        "last checkpoint nudge: never",
    )

    def write_state(self, sessions):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        doc = {"schemaVersion": 1, "servers": {}, "sessions": sessions}
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)

    def raw(self):
        with open(self.state_file, "rb") as fh:
            return fh.read()

    def session(self, **fields):
        base = {
            "epoch": 0,
            "flags": {},
            "gitianReads": 0,
            "edits": 0,
            "publishes": 0,
            "lastSeenVocabRev": {},
            "lintHashes": [],
            "mintPrompted": [],
            "updatedAt": _iso_from_now(),
        }
        base.update(fields)
        return base

    def status(self):
        proc = self.run_state("status")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(proc.stderr, "")
        return proc.stdout

    def test_open_activity_the_watch_and_the_checkpoints_are_printed(self):
        ledger_doc = {
            "events": [
                {
                    "at": _iso_from_now(minutes=-20),
                    "kind": "commit",
                    "repo": "acme/widgets",
                    "ref": "abc1234",
                    "text": "Tweak the thing",
                }
            ],
            "edits": [
                {
                    "min": _iso_from_now(minutes=-10)[:16],
                    "at": _iso_from_now(minutes=-10),
                    "n": 3,
                    "repo": "acme/widgets",
                }
            ],
            "checkpointAt": _iso_from_now(minutes=-90),
            "briefAt": _iso_from_now(minutes=-40),  # past its grace: covers nothing
        }
        watch = [
            {"repo": "acme/widgets", "number": 12, "why": "opened", "addedAt": _iso_from_now(minutes=-60)},
            {"repo": "acme/widgets", "branch": "feat/x", "why": "branch", "addedAt": _iso_from_now(minutes=-30)},
        ]
        self.write_state(
            {"sidS": self.session(ledger=ledger_doc, watch=watch, nudges={"at": _iso_from_now(hours=-3)})}
        )
        before = self.raw()

        out = self.status()

        self.assertRegex(out, self.HEADLINE)
        self.assertIn('\n  - acme/widgets: 1 commit (abc1234 "Tweak the thing"); 3 file edits\n', out)
        self.assertIn(
            "\n  watching for a merge: acme/widgets#12 (opened), acme/widgets@feat/x (branch)\n", out
        )
        self.assertIn(
            "\n  last KB write covering activity: 1h ago; last scribe brief: 40m ago; "
            "last checkpoint nudge: 3h ago\n",
            out,
        )
        # they follow the session line, in that order
        positions = [
            out.index("current session sidS"),
            out.index("activity no KB write covers yet"),
            out.index("watching for a merge"),
            out.index("last KB write covering activity"),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(self.raw(), before)  # status reads, it never writes

    def test_a_fresh_session_says_there_is_nothing(self):
        self.write_state({"sidE": self.session(ledger={}, watch=[], nudges={})})
        out = self.status()
        for line in self.EMPTY_LINES:
            self.assertIn("\n" + line + "\n", out)

    def test_activity_a_write_already_covers_is_not_listed(self):
        covered = {
            "events": [{"at": _iso_from_now(minutes=-30), "kind": "commit", "repo": "acme/widgets"}],
            "checkpointAt": _iso_from_now(minutes=-10),
        }
        self.write_state({"sidC": self.session(ledger=covered)})
        out = self.status()
        self.assertIn("\n  activity no KB write covers yet: none\n", out)
        self.assertIn("last KB write covering activity: 10m ago", out)

    def test_a_session_from_an_older_install_prints_the_empty_lines(self):
        old = self.session()
        self.write_state({"sidO": old})
        out = self.status()
        for line in self.EMPTY_LINES:
            self.assertIn("\n" + line + "\n", out)

    def test_a_corrupt_ledger_prints_as_empty_and_status_does_not_write(self):
        corrupt = {
            "sidX": self.session(ledger="garbage", watch=7, nudges=["x"]),
            "sidY": self.session(
                ledger={
                    "events": [None, {"kind": "commit"}, {"kind": "commit", "at": "x"}, "e"],
                    "edits": "many",
                    "briefAt": 5,
                    "checkpointAt": [],
                    "dropped": "lots",
                },
                watch=[None, 1, {"repo": "nope"}, {"repo": "a/b", "number": True, "addedAt": "x"}],
                nudges={"at": 5, "pollAt": "soon"},
            ),
        }
        for sid, session in corrupt.items():
            with self.subTest(sid=sid):
                # make the one under test the most recently updated session
                session["updatedAt"] = _iso_from_now()
                other = "sidY" if sid == "sidX" else "sidX"
                stale = dict(corrupt[other], updatedAt=_iso_from_now(hours=-2))
                self.write_state({sid: session, other: stale})
                before = self.raw()
                out = self.status()
                self.assertIn("current session %s:" % sid, out)
                for line in self.EMPTY_LINES:
                    self.assertIn("\n" + line + "\n", out)
                self.assertEqual(self.raw(), before)

    def test_an_event_that_cannot_be_rendered_drops_only_the_ledger_lines(self):
        broken = {
            "events": [
                {"at": _iso_from_now(minutes=-5), "kind": "commit", "repo": ["not", "hashable"]}
            ]
        }
        self.write_state({"sidB": self.session(ledger=broken)})
        out = self.status()  # still exit 0 with nothing on stderr
        self.assertIn("current session sidB:", out)
        self.assertIn("sessions: 1 tracked", out)
        self.assertNotIn("activity no KB write", out)
        self.assertNotIn("watching for a merge", out)

    def test_with_no_session_there_are_no_ledger_lines(self):
        out = self.status()
        self.assertIn("sessions: 0 tracked", out)
        self.assertNotIn("activity no KB write", out)
        self.assertNotIn("watching for a merge", out)
        self.assertNotIn("last KB write covering activity", out)


if __name__ == "__main__":
    unittest.main()
