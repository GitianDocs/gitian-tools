#!/usr/bin/env python3
"""Unit tests for publish_lint.py, the gitian-kb nudge layer's PreToolUse publish lint.

Drives it end to end via `sh publish-lint.sh` (matching how hooks.json actually invokes it), with
GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_publish_lint.py
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
PUBLISH_LINT_SH = HOOKS_DIR / "publish-lint.sh"
STATE_PY = HOOKS_DIR / "state.py"

SERVER_KEY = "https://gitian.dev/api/mcp"  # default GITIAN_KB_URL, per the state contract
REASON_PREFIX = "gitian-kb publish lint (advisory - re-send the identical call to proceed unchanged):"


def envelope(tool_name, tool_input=None, session_id="sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {},
    }


class PublishLintTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-publish-lint-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env.pop("GITIAN_KB_URL", None)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_lint(self, payload):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(PUBLISH_LINT_SH)],
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

    def seed_vocab(self, topics):
        doc = {"servers": {SERVER_KEY: {"topics": topics}}}
        proc = self.run_state("merge", input_text=json.dumps(doc))
        self.assertEqual(proc.returncode, 0)

    def bump_epoch(self, sid="sess-1"):
        proc = self.run_state("bump-epoch", sid)
        self.assertEqual(proc.returncode, 0)

    def dump_state(self):
        proc = self.run_state("dump")
        self.assertEqual(proc.returncode, 0)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertEqual(proc.stdout, "")

    def assert_denied(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        body = json.loads(proc.stdout)
        hook_output = body["hookSpecificOutput"]
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        self.assertEqual(hook_output["permissionDecision"], "deny")
        reason = hook_output["permissionDecisionReason"]
        self.assertTrue(reason.startswith(REASON_PREFIX), msg=reason)
        return reason


class GuardClause(PublishLintTestCase):
    def test_non_gitian_tool_is_silent_and_untouched(self):
        proc = self.run_lint(envelope("Write", tool_input={"topics": []}))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_empty_stdin_is_silent(self):
        proc = self.run_lint("")
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_garbage_stdin_is_silent(self):
        proc = self.run_lint("{not valid json ][ at all")
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_missing_session_id_is_silent(self):
        payload = envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={})
        del payload["session_id"]
        proc = self.run_lint(payload)
        self.assert_silent(proc)

    def test_corrupt_state_file_is_survived_and_rebuilt(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")

        proc = self.run_lint(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"title": "x"})
        )
        self.assert_denied(proc)

        state = self.dump_state()
        self.assertEqual(state.get("schemaVersion"), 1)
        self.assertTrue(state["sessions"]["sess-1"]["flags"]["lint_empty_topics"])


class EmptyTopicsRule(PublishLintTestCase):
    def test_fires_once_with_candidates_from_cached_vocab(self):
        self.seed_vocab(
            [
                {"slug": "auth", "description": "Authentication flows", "degree": 5},
                {"slug": "billing", "description": "Billing & invoices", "degree": 3},
                {"slug": "kb", "description": "Knowledge base", "degree": 1},
            ]
        )
        proc = self.run_lint(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"title": "x"})
        )
        reason = self.assert_denied(proc)
        self.assertIn("auth - Authentication flows", reason)
        self.assertIn("billing - Billing & invoices", reason)
        self.assertIn("kb - Knowledge base", reason)

        state = self.dump_state()
        session = state["sessions"]["sess-1"]
        self.assertTrue(session["flags"]["lint_empty_topics"])
        self.assertEqual(len(session["lintHashes"]), 1)

    def test_missing_topics_key_also_fires(self):
        proc = self.run_lint(envelope("mcp__plugin_gitian-kb_gitian__publish_memory", tool_input={}))
        self.assert_denied(proc)

    def test_empty_vocab_cache_advises_reading_vocab_resource(self):
        proc = self.run_lint(envelope("mcp__plugin_gitian-kb_gitian__publish_memory", tool_input={}))
        reason = self.assert_denied(proc)
        self.assertIn("gitian-kb://vocab", reason)
        self.assertIn("1-3 topics", reason)

    def test_populated_topics_does_not_fire(self):
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"title": "x", "topics": ["auth"]},
            )
        )
        self.assert_silent(proc)

    def test_identical_resend_passes_silently(self):
        payload = envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"title": "x"})
        first = self.run_lint(payload)
        self.assert_denied(first)

        second = self.run_lint(payload)
        self.assert_silent(second)

        # A silent identical retry must not append a second hash.
        state = self.dump_state()
        self.assertEqual(len(state["sessions"]["sess-1"]["lintHashes"]), 1)

    def test_corrected_call_passes(self):
        first = self.run_lint(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"title": "x"})
        )
        self.assert_denied(first)

        second = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"title": "x", "topics": ["auth"]},
            )
        )
        self.assert_silent(second)

    def test_different_offending_call_silent_after_flag_consumed(self):
        first = self.run_lint(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"title": "a"})
        )
        self.assert_denied(first)

        # A different call (different tool_input -> different hash) that still has empty topics --
        # the rule's flag was already consumed this epoch, so this must be silent.
        second = self.run_lint(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"title": "b"})
        )
        self.assert_silent(second)

    def test_epoch_bump_rearms_the_rule(self):
        first = self.run_lint(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"title": "a"})
        )
        self.assert_denied(first)

        self.bump_epoch("sess-1")

        # Same offending shape as before the bump -- epoch bump cleared both flags and
        # lintHashes, so this is neither a suppressed-by-flag nor a suppressed-by-hash case.
        second = self.run_lint(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"title": "a"})
        )
        self.assert_denied(second)


class AppendEntryExemption(PublishLintTestCase):
    def test_no_topics_is_not_flagged_by_r1(self):
        proc = self.run_lint(
            envelope("mcp__plugin_gitian-kb_gitian__append_entry", tool_input={"slug": "journal-x"})
        )
        self.assert_silent(proc)

    def test_near_miss_topic_still_caught_by_r2(self):
        self.seed_vocab([{"slug": "kb-discipline", "description": "KB discipline", "degree": 4}])
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__append_entry",
                tool_input={"slug": "journal-x", "topics": ["kb-disciplne"]},
            )
        )
        reason = self.assert_denied(proc)
        self.assertIn('"kb-disciplne"', reason)
        self.assertIn('"kb-discipline"', reason)

    def test_project_name_topic_still_caught_by_r3(self):
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__append_entry",
                tool_input={"slug": "journal-x", "project": "gitian", "topics": ["gitian"]},
            )
        )
        reason = self.assert_denied(proc)
        self.assertIn('"gitian"', reason)


class NearMissRule(PublishLintTestCase):
    def test_fires_with_did_you_mean_suggestion(self):
        self.seed_vocab([{"slug": "kb-discipline", "description": "KB discipline", "degree": 4}])
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["kb-disciplne"]},
            )
        )
        reason = self.assert_denied(proc)
        self.assertIn('did you mean "kb-discipline"?', reason)

    def test_checks_mentions_field_too(self):
        self.seed_vocab([{"slug": "kb-discipline", "description": "KB discipline", "degree": 4}])
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["kb-discipline"], "mentions": ["kb-disciplne"]},
            )
        )
        reason = self.assert_denied(proc)
        self.assertIn('did you mean "kb-discipline"?', reason)

    def test_exact_cached_slug_does_not_fire(self):
        self.seed_vocab([{"slug": "kb-discipline", "description": "KB discipline", "degree": 4}])
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["kb-discipline"]},
            )
        )
        self.assert_silent(proc)

    def test_empty_cache_disables_the_check(self):
        # Non-empty topics (so r1 is out of play) but no cached vocab at all -- r2's guard
        # ("only when the cache is non-empty") means no near-miss check runs.
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["anything"]},
            )
        )
        self.assert_silent(proc)

    def test_far_slug_beyond_distance_two_does_not_fire(self):
        self.seed_vocab([{"slug": "kb-discipline", "description": "KB discipline", "degree": 4}])
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["completely-different-slug"]},
            )
        )
        self.assert_silent(proc)


class ProjectNameRule(PublishLintTestCase):
    def test_fires_when_topic_equals_project(self):
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"project": "gitian", "topics": ["gitian"]},
            )
        )
        reason = self.assert_denied(proc)
        self.assertIn('"gitian"', reason)

    def test_fires_when_topic_equals_repo_basename(self):
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"repo": "GitianDocs/gitian-kb", "topics": ["gitian-kb"]},
            )
        )
        reason = self.assert_denied(proc)
        self.assertIn('"gitian-kb"', reason)

    def test_case_insensitive_match(self):
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"project": "Gitian", "topics": ["gitian"]},
            )
        )
        reason = self.assert_denied(proc)
        self.assertIn("gitian", reason.lower())

    def test_no_project_or_repo_does_not_fire(self):
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["gitian"]},
            )
        )
        self.assert_silent(proc)

    def test_unrelated_topic_does_not_fire(self):
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"project": "gitian", "topics": ["auth"]},
            )
        )
        self.assert_silent(proc)


class MultipleRulesTogether(PublishLintTestCase):
    def test_near_miss_and_project_name_both_bullet_in_one_reason(self):
        self.seed_vocab([{"slug": "kb-discipline", "description": "KB discipline", "degree": 4}])
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={
                    "project": "gitian",
                    "topics": ["kb-disciplne", "gitian"],
                },
            )
        )
        reason = self.assert_denied(proc)
        self.assertIn('did you mean "kb-discipline"?', reason)
        self.assertIn('"gitian"', reason)

        state = self.dump_state()
        flags = state["sessions"]["sess-1"]["flags"]
        self.assertTrue(flags["lint_near_miss"])
        self.assertTrue(flags["lint_project_topic"])
        self.assertNotIn("lint_empty_topics", flags)  # topics list was non-empty -- r1 never matched


if __name__ == "__main__":
    unittest.main()
