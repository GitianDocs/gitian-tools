#!/usr/bin/env python3
"""Unit tests for orientation-check.sh, the gitian-kb nudge layer's PreToolUse orientation nudge.

Drives it end to end via `sh orientation-check.sh` (matching how hooks.json would invoke it, once
T12 registers it on matcher "Edit|Write|NotebookEdit"), with GITIAN_KB_STATE_FILE pointed at a
fresh tempdir per test so runs never touch a real ~/.claude/gitian-kb/state.json and never
interfere with each other.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_orientation.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
ORIENTATION_SH = HOOKS_DIR / "orientation-check.sh"
STATE_PY = HOOKS_DIR / "state.py"


def envelope(tool_name="Edit", tool_input=None, session_id="sess-1", cwd="/tmp"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {"file_path": "/x"},
    }


class OrientationTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-orientation-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_hook(self, payload):
        # payload's own "cwd" JSON field (not this process's real working directory) is what the
        # hook uses for `git -C <cwd> remote get-url origin` -- see RepoClause below.
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(ORIENTATION_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

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
        return proc

    def dump_state(self):
        proc = self.run_state("dump")
        self.assertEqual(proc.returncode, 0)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stdout=%r stderr=%r" % (proc.stdout, proc.stderr))
        self.assertEqual(proc.stdout, "")

    def assert_deny(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        return out["permissionDecisionReason"]


class ZeroReadsFiresOnce(OrientationTestCase):
    def test_first_mutation_with_zero_reads_denies_with_advisory_reason(self):
        proc = self.run_hook(envelope())
        reason = self.assert_deny(proc)
        self.assertIn("file_intents", reason)
        self.assertIn("search", reason)
        self.assertIn("neighbors", reason)
        self.assertIn("advisory", reason.lower())
        self.assertIn("re-send", reason.lower())

        state = self.dump_state()
        session = state["sessions"]["sess-1"]
        self.assertTrue(session["flags"]["orientation"])
        # The denied call itself must not be counted as a passed-through edit.
        self.assertEqual(session.get("edits", 0), 0)

    def test_identical_resend_passes_silently_and_increments_edits(self):
        first = self.run_hook(envelope())
        self.assert_deny(first)

        second = self.run_hook(envelope())
        self.assert_silent(second)

        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["edits"], 1)

    def test_fires_at_most_once_across_many_calls(self):
        self.run_hook(envelope())
        for _ in range(3):
            proc = self.run_hook(envelope())
            self.assert_silent(proc)
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["edits"], 3)


class RepoClause(OrientationTestCase):
    def test_derivable_repo_is_named_in_the_reason(self):
        repo_dir = tempfile.mkdtemp(prefix="gks-orientation-repo-")
        try:
            subprocess.run(["git", "init", "-q", repo_dir], check=True)
            subprocess.run(
                ["git", "-C", repo_dir, "remote", "add", "origin", "git@github.com:acme/widgets.git"],
                check=True,
            )
            proc = self.run_hook(envelope(cwd=repo_dir))
            reason = self.assert_deny(proc)
            self.assertIn("acme/widgets", reason)
        finally:
            shutil.rmtree(repo_dir, ignore_errors=True)

    def test_underivable_repo_omits_the_repo_clause(self):
        plain_dir = tempfile.mkdtemp(prefix="gks-orientation-plain-")
        try:
            proc = self.run_hook(envelope(cwd=plain_dir))
            reason = self.assert_deny(proc)
            self.assertIn("file_intents", reason)
            self.assertNotIn(" on ", reason.split("consider")[1].split("plus")[0])
        finally:
            shutil.rmtree(plain_dir, ignore_errors=True)


class NonZeroReadsAlwaysSilent(OrientationTestCase):
    def test_never_denies_from_the_start_and_still_counts_edits(self):
        self.merge({"sessions": {"sess-1": {"gitianReads": 3}}})

        first = self.run_hook(envelope())
        self.assert_silent(first)
        second = self.run_hook(envelope())
        self.assert_silent(second)

        state = self.dump_state()
        session = state["sessions"]["sess-1"]
        self.assertEqual(session["edits"], 2)
        self.assertNotIn("orientation", session.get("flags", {}))


class MissingSessionRecord(OrientationTestCase):
    def test_absent_session_behaves_as_zero_reads_and_fires(self):
        # A totally fresh sid, never touched in state before -- must be treated identically to
        # an explicit gitianReads=0, not silently skipped just because the key is absent.
        self.assertFalse(os.path.exists(self.state_file))
        proc = self.run_hook(envelope(session_id="brand-new-sid"))
        reason = self.assert_deny(proc)
        self.assertIn("file_intents", reason)

    def test_absent_session_among_other_tracked_sessions_still_fires(self):
        self.merge({"sessions": {"other-sid": {"gitianReads": 5}}})
        proc = self.run_hook(envelope(session_id="fresh-sid"))
        self.assert_deny(proc)


class EpochBumpRearms(OrientationTestCase):
    def test_epoch_bump_clears_the_flag_and_resets_reads_so_it_fires_again(self):
        first = self.run_hook(envelope())
        self.assert_deny(first)

        resend = self.run_hook(envelope())
        self.assert_silent(resend)

        bump = self.run_state("bump-epoch", "sess-1")
        self.assertEqual(bump.returncode, 0)

        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["flags"], {})
        self.assertEqual(state["sessions"]["sess-1"]["gitianReads"], 0)

        again = self.run_hook(envelope())
        self.assert_deny(again)


class GuardClause(OrientationTestCase):
    def test_empty_stdin_is_silent(self):
        proc = self.run_hook("")
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_garbage_stdin_is_silent(self):
        proc = self.run_hook("{not valid json ][ at all")
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_missing_session_id_is_silent(self):
        proc = self.run_hook({"cwd": "/tmp", "tool_name": "Edit"})
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_non_object_stdin_is_silent(self):
        proc = self.run_hook("[1, 2, 3]")
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))


class CorruptStateFailOpen(OrientationTestCase):
    def test_unwritable_state_directory_is_silent(self):
        # A regular file occupies the path component the state dir would need to become --
        # os.makedirs() inside state.py can't create it, so every gks_* call fails silently
        # (state.py's own fail-open contract) and the hook must stay silent end to end, never
        # emitting a partial/stray denial.
        blocker = os.path.join(self.tmpdir, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        self.env["GITIAN_KB_STATE_FILE"] = os.path.join(blocker, "nested", "state.json")

        proc = self.run_hook(envelope(session_id="corrupt-sid"))
        self.assert_silent(proc)


if __name__ == "__main__":
    unittest.main()
