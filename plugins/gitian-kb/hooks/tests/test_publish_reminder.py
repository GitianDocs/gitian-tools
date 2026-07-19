#!/usr/bin/env python3
"""Unit tests for publish_reminder.py, the gitian-kb nudge layer's Stop-hook publish reminder.

Drives it end to end via `sh publish-reminder.sh` (matching how hooks.json actually invokes it),
with GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test and a synthetic transcript JSONL
file per test, so runs never touch a real ~/.claude/gitian-kb/state.json and never interfere with
each other.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_publish_reminder.py
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
PUBLISH_REMINDER_SH = HOOKS_DIR / "publish-reminder.sh"
STATE_PY = HOOKS_DIR / "state.py"

SID = "sess-1"


def _tool_use(name, tool_input=None):
    return {"type": "tool_use", "name": name, "input": tool_input if tool_input is not None else {}}


def _assistant_line(blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def edit_line(name="Edit"):
    return _assistant_line([_tool_use(name, {"file_path": "/x", "old_string": "a", "new_string": "b"})])


def bash_line(command):
    return _assistant_line([_tool_use("Bash", {"command": command, "description": "run"})])


def publish_line(tool_name="mcp__plugin_gitian-kb_gitian__publish_doc"):
    return _assistant_line([_tool_use(tool_name, {"slug": "x"})])


class PublishReminderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-publish-reminder-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env.pop("GITIAN_KB_URL", None)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- transcript helpers ---------------------------------------------------------------

    def write_transcript(self, line_objs):
        return self.write_raw_transcript(json.dumps(obj) for obj in line_objs)

    def write_raw_transcript(self, raw_lines):
        path = os.path.join(self.tmpdir, "transcript-%d.jsonl" % len(os.listdir(self.tmpdir)))
        with open(path, "w", encoding="utf-8") as fh:
            for line in raw_lines:
                fh.write(line)
                fh.write("\n")
        return path

    # --- envelope / invocation --------------------------------------------------------------

    def envelope(self, transcript_path=None, session_id=SID, stop_hook_active=False):
        return {
            "session_id": session_id,
            "transcript_path": transcript_path,
            "cwd": "/repo",
            "hook_event_name": "Stop",
            "stop_hook_active": stop_hook_active,
        }

    def run_hook(self, payload):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(PUBLISH_REMINDER_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

    def bump_epoch(self, sid):
        proc = subprocess.run(
            [sys.executable, str(STATE_PY), "bump-epoch", sid],
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

    def flag(self, sid, name="publish_reminder"):
        state = self.dump_state()
        return state.get("sessions", {}).get(sid, {}).get("flags", {}).get(name)

    # --- assertions ------------------------------------------------------------------------

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertEqual(proc.stdout, "")

    def assert_block(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload.get("decision"), "block")
        self.assertTrue(payload.get("reason"))
        return payload


class BelowThreshold(PublishReminderTestCase):
    def test_two_edits_no_commits_is_silent_and_flag_unconsumed(self):
        transcript = self.write_transcript([edit_line(), edit_line()])
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_silent(proc)
        self.assertIsNone(self.flag(SID))

        # The flag must NOT have been consumed -- a later turn-end that crosses the bar (one
        # more edit) must still be able to fire.
        transcript2 = self.write_transcript([edit_line(), edit_line(), edit_line()])
        proc2 = self.run_hook(self.envelope(transcript_path=transcript2))
        self.assert_block(proc2)


class ThresholdCrossing(PublishReminderTestCase):
    def test_three_edits_blocks_exactly_once(self):
        transcript = self.write_transcript([edit_line()] * 3)
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        payload = self.assert_block(proc)
        self.assertIn("3 file edits", payload["reason"])

        proc2 = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_silent(proc2)
        self.assertTrue(self.flag(SID))

    def test_one_commit_blocks(self):
        transcript = self.write_transcript([bash_line("git commit -m 'wip'")])
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        payload = self.assert_block(proc)
        self.assertIn("a commit", payload["reason"])

    def test_chained_git_commit_after_add_blocks(self):
        transcript = self.write_transcript([bash_line("git add -A && git commit -m 'wip'")])
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_block(proc)

    def test_git_log_grep_commit_is_not_a_commit_action(self):
        # Regression: "commit" merely appears as a --grep argument, not the subcommand.
        transcript = self.write_transcript([bash_line("git log --grep commit")])
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_silent(proc)

    def test_commit_word_in_prose_is_not_a_commit_action(self):
        transcript = self.write_transcript([bash_line("echo 'please commit later'")])
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_silent(proc)

    def test_gh_pr_create_with_quoted_git_commit_body_is_not_a_commit_action(self):
        # Regression: the old naive whitespace token scan counted a MERE QUOTED MENTION of "git
        # commit" (inside another command's --body argument) as a commit action -- a spurious
        # Stop block. The matcher now delegates to commit_nudge.py's shlex-based segment
        # tokenizer, which keeps the quoted phrase as ONE token, so this must stay silent even
        # though the transcript's only Bash command is this single gh pr create call.
        transcript = self.write_transcript(
            [bash_line('gh pr create --body "after review, run git commit again"')]
        )
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_silent(proc)
        self.assertIsNone(self.flag(SID))


class PublishSuppression(PublishReminderTestCase):
    def test_any_gitian_publish_in_transcript_is_silent_even_with_edits(self):
        transcript = self.write_transcript([edit_line()] * 3 + [publish_line()])
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_silent(proc)
        self.assertIsNone(self.flag(SID))

    def test_append_entry_publish_also_suppresses(self):
        transcript = self.write_transcript(
            [bash_line("git commit -m 'wip'"), publish_line("mcp__plugin_gitian-kb_gitian__append_entry")]
        )
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_silent(proc)


class LoopGuard(PublishReminderTestCase):
    def test_stop_hook_active_short_circuits_even_when_crossed(self):
        transcript = self.write_transcript([edit_line()] * 3)
        proc = self.run_hook(self.envelope(transcript_path=transcript, stop_hook_active=True))
        self.assert_silent(proc)
        self.assertIsNone(self.flag(SID))


class EpochBump(PublishReminderTestCase):
    def test_epoch_bump_rearms_the_flag(self):
        transcript = self.write_transcript([edit_line()] * 3)
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_block(proc)

        proc2 = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_silent(proc2)

        self.bump_epoch(SID)

        proc3 = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_block(proc3)


class Robustness(PublishReminderTestCase):
    def test_missing_transcript_is_silent(self):
        proc = self.run_hook(
            self.envelope(transcript_path=os.path.join(self.tmpdir, "does-not-exist.jsonl"))
        )
        self.assert_silent(proc)
        self.assertIsNone(self.flag(SID))

    def test_garbage_lines_interleaved_still_counts_correctly(self):
        raw_lines = [
            "not json at all",
            json.dumps(edit_line()),
            "",
            "{broken json [",
            json.dumps(edit_line()),
            json.dumps({"type": "user", "message": {"content": "hi"}}),
            json.dumps(edit_line()),
        ]
        transcript = self.write_raw_transcript(raw_lines)
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_block(proc)

    def test_missing_session_id_is_silent(self):
        transcript = self.write_transcript([edit_line()] * 3)
        payload = self.envelope(transcript_path=transcript)
        del payload["session_id"]
        proc = self.run_hook(payload)
        self.assert_silent(proc)

    def test_empty_stdin_is_silent(self):
        proc = self.run_hook("")
        self.assert_silent(proc)

    def test_garbage_stdin_is_silent(self):
        proc = self.run_hook("{not valid json ][ at all")
        self.assert_silent(proc)

    def test_corrupt_state_path_collision_is_silent(self):
        # The state file path itself resolves to a directory (not a regular file) -- load() and
        # the initial peek survive this fine (both catch-and-treat-as-empty), but the later
        # locked read-modify-write inside _flag_once will raise trying to os.replace a tmp file
        # over a directory. That exception must be swallowed by the top-level guard: total
        # silence, exit 0, no partial nudge -- even though the transcript clearly crosses the bar.
        os.makedirs(self.state_file, exist_ok=True)
        transcript = self.write_transcript([edit_line()] * 3)
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_silent(proc)

    def test_corrupt_session_shape_is_survived(self):
        # Schema-corrupt (but JSON-valid) state: sessions.<sid> is a string, not an object. The
        # flag peek must read this as "not set" (not raise), and _touch_session must replace it
        # with a fresh session dict rather than crashing on it.
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump({"schemaVersion": 1, "servers": {}, "sessions": {SID: "garbage"}}, fh)

        transcript = self.write_transcript([edit_line()] * 3)
        proc = self.run_hook(self.envelope(transcript_path=transcript))
        self.assert_block(proc)


if __name__ == "__main__":
    unittest.main()
