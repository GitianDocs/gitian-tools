#!/usr/bin/env python3
"""Unit tests for commit_nudge.py, the gitian-kb nudge layer's commit-journaling nudge.

Drives it end to end via `sh commit-nudge.sh` (matching how hooks.json actually invokes it),
with GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_commit_nudge.py
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
COMMIT_NUDGE_SH = HOOKS_DIR / "commit-nudge.sh"
STATE_PY = HOOKS_DIR / "state.py"

SERVER_KEY = "https://gitian.dev/api/mcp"  # default GITIAN_KB_URL, per the state contract


def envelope(command, tool_response=None, session_id="sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": tool_response if tool_response is not None else {"stdout": "", "stderr": ""},
    }


def iso(delta):
    """ISO-8601 timestamp `delta` away from now (delta negative -> in the past)."""
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ")


class CommitNudgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-commit-nudge-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env.pop("GITIAN_KB_URL", None)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_hook(self, payload):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(COMMIT_NUDGE_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

    def merge(self, doc):
        proc = subprocess.run(
            [sys.executable, str(STATE_PY), "merge"],
            input=json.dumps(doc),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        return proc

    def dump_state(self):
        proc = subprocess.run(
            [sys.executable, str(STATE_PY), "dump"],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertEqual(proc.stdout, "")

    def assert_fired(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        self.assertIn("append_entry", out["additionalContext"])
        return out


class FiresOnceNoAppendRecorded(CommitNudgeTestCase):
    def test_fires_once_then_silent_on_repeat(self):
        first = self.run_hook(envelope("git commit -m 'fix things'"))
        self.assert_fired(first)

        state = self.dump_state()
        self.assertTrue(state["sessions"]["sess-1"]["flags"]["commit_nudge"])

        second = self.run_hook(envelope("git commit -m 'fix more things'"))
        self.assert_silent(second)

    def test_fires_with_stale_last_append_at(self):
        self.merge(
            {"servers": {SERVER_KEY: {"lastAppendAt": iso(-timedelta(hours=5))}}}
        )
        proc = self.run_hook(envelope("git commit -m 'fix things'"))
        self.assert_fired(proc)

    def test_fires_with_dash_capital_c_global_option(self):
        # `-C <path>` consumes its following argument as part of the option, not as the
        # subcommand -- the scan must still land on `commit` (regression case from adversarial
        # review: the option-argument was previously not skipped, so this never fired).
        proc = self.run_hook(envelope("git -C repo commit -m x"))
        self.assert_fired(proc)

    def test_fires_with_dash_lowercase_c_global_option(self):
        # Same for `-c <key=val>`.
        proc = self.run_hook(envelope("git -c user.name=x commit"))
        self.assert_fired(proc)

    def test_fires_with_newline_separated_compound_command(self):
        # Regression: a newline-separated compound command (as opposed to |/;/&&) must still be
        # segment-split and detected -- see commit_nudge.py's _SEGMENT_SPLIT_RE.
        self.merge({"servers": {SERVER_KEY: {"lastAppendAt": iso(-timedelta(hours=5))}}})
        proc = self.run_hook(envelope("git add -A\ngit commit -m x"))
        self.assert_fired(proc)


class DamperSuppressesWithoutConsumingFlag(CommitNudgeTestCase):
    def test_recent_append_silences_and_leaves_flag_unconsumed(self):
        self.merge(
            {"servers": {SERVER_KEY: {"lastAppendAt": iso(-timedelta(minutes=30))}}}
        )
        proc = self.run_hook(envelope("git commit -m 'fix things'"))
        self.assert_silent(proc)

        state = self.dump_state()
        session = state.get("sessions", {}).get("sess-1", {})
        self.assertFalse(session.get("flags", {}).get("commit_nudge"))

    def test_later_commit_with_stale_append_still_fires(self):
        self.merge(
            {"servers": {SERVER_KEY: {"lastAppendAt": iso(-timedelta(minutes=30))}}}
        )
        damped = self.run_hook(envelope("git commit -m 'first commit'"))
        self.assert_silent(damped)

        # Append ages past the 2h window -- a later commit should get its own chance to fire.
        self.merge(
            {"servers": {SERVER_KEY: {"lastAppendAt": iso(-timedelta(hours=3))}}}
        )
        later = self.run_hook(envelope("git commit -m 'second commit'"))
        self.assert_fired(later)


class NonCommitBashIsSilent(CommitNudgeTestCase):
    def test_plain_non_git_command(self):
        proc = self.run_hook(envelope("echo hello"))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_git_log_grep_commit_does_not_false_positive(self):
        # Contains both "git" and "commit" as separate words, but "commit" is an argument to
        # --grep, not the subcommand -- must not be mistaken for a commit action.
        proc = self.run_hook(envelope("git log --grep commit"))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_echo_mentioning_git_commit_does_not_false_positive(self):
        # Contains "git" and "commit" as separate whitespace tokens, but the command head is
        # `echo`, not `git` -- must not be mistaken for a commit action (regression case from
        # adversarial review: token-scanning without anchoring to the command head fired here).
        proc = self.run_hook(envelope("echo please git commit now"))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_chained_echo_mentioning_git_commit_does_not_false_positive(self):
        # Same false positive, but hiding in a later `&&`-chained segment.
        proc = self.run_hook(envelope("git add -A && echo next step is git commit"))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_gh_pr_create_with_quoted_git_commit_body_does_not_false_positive(self):
        # The quoted --body argument mentions "git commit", but the command is `gh pr create`,
        # not a commit action -- must not be mistaken for one.
        proc = self.run_hook(envelope('gh pr create --body "run git commit"'))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_word_commit_without_git_does_not_false_positive(self):
        proc = self.run_hook(envelope("npm run commit"))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_missing_command_field_is_silent(self):
        proc = self.run_hook(envelope(None))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_non_bash_tool_is_silent(self):
        payload = envelope("git commit -m x")
        payload["tool_name"] = "Edit"
        proc = self.run_hook(payload)
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))


class FailedCommitIsSilent(CommitNudgeTestCase):
    def test_nothing_to_commit(self):
        proc = self.run_hook(
            envelope(
                "git commit -m 'noop'",
                tool_response={"stdout": "nothing to commit, working tree clean\n", "stderr": ""},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_error_marker(self):
        proc = self.run_hook(
            envelope(
                "git commit -m 'noop'",
                tool_response={"stdout": "", "stderr": "error: pathspec did not match any files"},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_fatal_marker(self):
        proc = self.run_hook(
            envelope(
                "git commit -m 'noop'",
                tool_response={"stdout": "", "stderr": "fatal: not a git repository"},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_aborting_commit_marker_case_insensitive(self):
        # Real git output capitalizes this ("Aborting commit due to empty commit message") --
        # the failure guard must match case-insensitively.
        proc = self.run_hook(
            envelope(
                "git commit",
                tool_response={"stdout": "", "stderr": "Aborting commit due to empty commit message.\n"},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_failed_commit_does_not_burn_the_once_flag(self):
        failed = self.run_hook(
            envelope(
                "git commit -m 'noop'",
                tool_response={"stdout": "", "stderr": "error: nope"},
            )
        )
        self.assert_silent(failed)

        succeeded = self.run_hook(envelope("git commit -m 'real change'"))
        self.assert_fired(succeeded)


class GhPrMerge(CommitNudgeTestCase):
    def test_gh_pr_merge_triggers(self):
        proc = self.run_hook(envelope("gh pr merge 123 --squash"))
        self.assert_fired(proc)

    def test_gh_pr_view_does_not_trigger(self):
        proc = self.run_hook(envelope("gh pr view 123"))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))


class GuardClauseRobustness(CommitNudgeTestCase):
    def test_empty_stdin_is_silent(self):
        proc = self.run_hook("")
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_garbage_stdin_is_silent(self):
        proc = self.run_hook("{not valid json ][ at all")
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_corrupt_state_with_non_commit_command_is_silent(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")

        proc = self.run_hook(envelope("git log --grep commit"))
        self.assert_silent(proc)

    def test_corrupt_state_with_real_commit_recovers_and_fires(self):
        # A corrupt pre-existing state file must not crash the hook -- state.py's own load()
        # normalizes it to a fresh, valid v1 state (same recovery contract harvest.py relies on),
        # so a genuine commit action with no recorded append still gets its nudge.
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")

        proc = self.run_hook(envelope("git commit -m 'recovered'"))
        self.assert_fired(proc)

        state = self.dump_state()
        self.assertEqual(state.get("schemaVersion"), 1)
        self.assertTrue(state["sessions"]["sess-1"]["flags"]["commit_nudge"])


if __name__ == "__main__":
    unittest.main()
