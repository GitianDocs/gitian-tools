#!/usr/bin/env python3
"""Unit tests for state.py, the gitian-kb nudge layer's state substrate.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_state.py
Every test points GITIAN_KB_STATE_FILE at a fresh tempdir so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other.
"""

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
STATE_PY = HOOKS_DIR / "state.py"


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
                        "lastSeenVocabRev": 5,
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
        self.assertEqual(after["lastSeenVocabRev"], 5)  # preserved across epoch bump

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
                    "lastSeenVocabRev": None,
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
                    "lastSeenVocabRev": None,
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


if __name__ == "__main__":
    unittest.main()
