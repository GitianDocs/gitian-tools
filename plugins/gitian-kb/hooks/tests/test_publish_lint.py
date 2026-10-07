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
CONTEXT_PREFIX = "gitian-kb publish lint (advisory -- the call was not blocked):"
NEAR_MISS_VOCAB = [{"slug": "kb-discipline", "description": "KB discipline", "degree": 4}]


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

    def assert_advised(self, proc):
        """The lint's ONLY output shape: PreToolUse additionalContext and nothing that decides.
        No permissionDecision at all -- not "deny" (that blocked delegated writers), and never
        "allow" (that would skip the user's own permission prompts)."""
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        body = json.loads(proc.stdout)
        self.assertEqual(set(body), {"hookSpecificOutput"})
        hook_output = body["hookSpecificOutput"]
        self.assertEqual(set(hook_output), {"hookEventName", "additionalContext"})
        self.assertEqual(hook_output["hookEventName"], "PreToolUse")
        context = hook_output["additionalContext"]
        self.assertTrue(context.startswith(CONTEXT_PREFIX), msg=context)
        self.assertNotIn("re-send", context)
        return context


class GuardClause(PublishLintTestCase):
    def test_non_gitian_tool_is_silent_and_untouched(self):
        proc = self.run_lint(envelope("Write", tool_input={"topics": ["kb-disciplne"]}))
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
        payload = envelope(
            "mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"topics": ["kb-disciplne"]}
        )
        del payload["session_id"]
        proc = self.run_lint(payload)
        self.assert_silent(proc)

    def test_corrupt_state_file_is_survived_without_advice(self):
        # A corrupt file loads as an empty state: no cached vocabulary, so nothing to compare a
        # slug against -- silent, never a traceback.
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")

        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"topics": ["kb-disciplne"]}
            )
        )
        self.assert_silent(proc)


class NeverBlocks(PublishLintTestCase):
    """The whole point of 0.24.0: an advisory lint must never stand between a writer and a
    publish. A delegated writer (kb-scribe) read the old once-per-rule deny as a refusal."""

    def test_firing_lint_emits_context_and_no_permission_decision(self):
        self.seed_vocab(NEAR_MISS_VOCAB)
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["kb-disciplne"]},
            )
        )
        context = self.assert_advised(proc)
        self.assertNotIn("permissionDecision", proc.stdout)
        # The call already went through, so the remedy is a follow-up revision, not a re-send.
        self.assertIn("`patch_doc`/`patch_memory`", context)
        self.assertIn("`retract_topic`", context)


class ServerDuplicatesRetired(PublishLintTestCase):
    """Rules the server already returns as warnings in the same tool response are gone: empty
    topics (`no_topics`) and a project/repo-name topic (`project_name_topic`)."""

    def test_empty_topics_is_left_to_the_server(self):
        self.seed_vocab(NEAR_MISS_VOCAB)
        for tool in ("publish_doc", "publish_memory", "publish_entry"):
            with self.subTest(tool=tool):
                proc = self.run_lint(
                    envelope("mcp__plugin_gitian-kb_gitian__%s" % tool, tool_input={"title": "x"})
                )
                self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file) and self.dump_state().get("sessions"))

    def test_project_name_topic_is_left_to_the_server(self):
        for tool_input in (
            {"project": "gitian", "topics": ["gitian"]},
            {"repo": "GitianDocs/gitian-kb", "topics": ["gitian-kb"]},
        ):
            with self.subTest(tool_input=tool_input):
                proc = self.run_lint(
                    envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_input=tool_input)
                )
                self.assert_silent(proc)


class NearMissRule(PublishLintTestCase):
    def test_fires_with_did_you_mean_suggestion(self):
        self.seed_vocab(NEAR_MISS_VOCAB)
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["kb-disciplne"]},
            )
        )
        context = self.assert_advised(proc)
        self.assertIn('"kb-disciplne" -- did you mean "kb-discipline"?', context)
        self.assertTrue(self.dump_state()["sessions"]["sess-1"]["flags"]["lint_near_miss"])

    def test_checks_mentions_field_too(self):
        self.seed_vocab(NEAR_MISS_VOCAB)
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["kb-discipline"], "mentions": ["kb-disciplne"]},
            )
        )
        context = self.assert_advised(proc)
        self.assertIn('did you mean "kb-discipline"?', context)

    def test_append_entry_and_patch_tools_are_covered(self):
        # A patch REPLACES a manifest list wholesale and an append union-merges one, so a mistyped
        # slug is exactly as reachable there as on a publish.
        for tool in ("append_entry", "patch_doc", "patch_memory", "publish_entry"):
            with self.subTest(tool=tool):
                self.bump_epoch("sess-1")
                self.seed_vocab(NEAR_MISS_VOCAB)
                proc = self.run_lint(
                    envelope(
                        "mcp__plugin_gitian-kb_gitian__%s" % tool,
                        tool_input={"slug": "x", "topics": ["kb-disciplne"]},
                    )
                )
                self.assert_advised(proc)

    def test_exact_cached_slug_does_not_fire(self):
        self.seed_vocab(NEAR_MISS_VOCAB)
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["kb-discipline"]},
            )
        )
        self.assert_silent(proc)

    def test_empty_cache_disables_the_check(self):
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["anything"]},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_far_slug_beyond_distance_two_does_not_fire(self):
        self.seed_vocab(NEAR_MISS_VOCAB)
        proc = self.run_lint(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"topics": ["completely-different-slug"]},
            )
        )
        self.assert_silent(proc)

    def test_fires_once_per_epoch_and_an_epoch_bump_rearms_it(self):
        self.seed_vocab(NEAR_MISS_VOCAB)
        payload = envelope(
            "mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"topics": ["kb-disciplne"]}
        )
        self.assert_advised(self.run_lint(payload))

        # The same advice again, or a different near miss, stays quiet this epoch.
        self.assert_silent(self.run_lint(payload))
        self.assert_silent(
            self.run_lint(
                envelope(
                    "mcp__plugin_gitian-kb_gitian__publish_doc",
                    tool_input={"topics": ["kb-disciplin"]},
                )
            )
        )

        self.bump_epoch("sess-1")
        self.assert_advised(self.run_lint(payload))

    def test_writes_no_lint_hash(self):
        # Nothing is ever denied, so there is no retry to recognise.
        self.seed_vocab(NEAR_MISS_VOCAB)
        self.assert_advised(
            self.run_lint(
                envelope(
                    "mcp__plugin_gitian-kb_gitian__publish_doc",
                    tool_input={"topics": ["kb-disciplne"]},
                )
            )
        )
        self.assertEqual(self.dump_state()["sessions"]["sess-1"]["lintHashes"], [])


if __name__ == "__main__":
    unittest.main()
