#!/usr/bin/env python3
"""Unit tests for harvest.py's mint-description follow-up (T8).

Drives harvest.py end to end via `sh harvest.sh` (the same invocation the PostToolUse hook uses),
with GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other. Scoped strictly to the
organic_topics_minted follow-up added in this task -- T2's harvesting behavior (vocab_rev, reads,
publishes) is covered by test_harvest.py, which must keep passing unchanged.

The real server warning (linksWarnings() in src/lib/kb/mcp-server.ts, documented in
docs/kb-mcp-transport.md) is a LintWarning object nested in the MCP content-block text:
  {"content": [{"type": "text",
                "text": "{... , \"warnings\": [{\"code\": \"organic_topics_minted\",
                                                  \"path\": \"topics\",
                                                  \"note\": \"auto-minted as organic, live
                                                             immediately: slug-a, slug-b\"}]}"}]}
`minted_response()` below builds exactly that shape; a couple of tests also cover the defensive
fallback (a literal "organic_topics_minted" JSON member) described in the task spec.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_mint_followup.py
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
HARVEST_SH = HOOKS_DIR / "harvest.sh"
STATE_PY = HOOKS_DIR / "state.py"

SERVER_KEY = "https://gitian.dev/api/mcp"  # default GITIAN_KB_URL, per the state contract


def envelope(tool_name, tool_input=None, tool_response=None, session_id="sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {},
        "tool_response": tool_response if tool_response is not None else {},
    }


def minted_response(slugs, slug="new-doc"):
    """The real MCP wire shape: an outer {"content": [...]} envelope wrapping a JSON-encoded
    text block, itself carrying a "warnings" array with the organic_topics_minted LintWarning."""
    inner = {
        "slug": slug,
        "warnings": [
            {
                "code": "organic_topics_minted",
                "path": "topics",
                "note": "auto-minted as organic, live immediately: " + ", ".join(slugs),
            }
        ],
    }
    return {"content": [{"type": "text", "text": json.dumps(inner)}], "isError": False}


class MintFollowupTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-mint-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env.pop("GITIAN_KB_URL", None)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_harvest(self, payload):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(HARVEST_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

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

    def additional_context(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        return out["hookSpecificOutput"]["additionalContext"]


class FiresOnNewSlugs(MintFollowupTestCase):
    def test_names_minted_slugs_and_records_state(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=minted_response(["auth-flow", "billing-edge"]),
            )
        )
        context = self.additional_context(proc)
        self.assertIn("auth-flow", context)
        self.assertIn("billing-edge", context)
        self.assertIn("publish_topic", context)

        state = self.dump_state()
        session = state["sessions"]["sess-1"]
        self.assertEqual(sorted(session["mintPrompted"]), ["auth-flow", "billing-edge"])
        undescribed = state["servers"][SERVER_KEY]["undescribedTopics"]
        self.assertEqual(sorted(undescribed), ["auth-flow", "billing-edge"])

    def test_single_slug_message_is_singular(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__append_entry",
                tool_response=minted_response(["auth-flow"]),
            )
        )
        context = self.additional_context(proc)
        self.assertIn("topic auth-flow was auto-minted", context)

    def test_defensive_fallback_literal_member_list_of_strings(self):
        # Not the real server shape, but the task spec's generic "organic_topics_minted is a
        # JSON member" description -- kept as a defensive fallback.
        response = {"isError": False, "organic_topics_minted": ["auth-flow", "billing-edge"]}
        proc = self.run_harvest(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_response=response)
        )
        context = self.additional_context(proc)
        self.assertIn("auth-flow", context)
        self.assertIn("billing-edge", context)

    def test_defensive_fallback_literal_member_list_of_slug_objects(self):
        response = {
            "isError": False,
            "organic_topics_minted": [{"slug": "auth-flow"}, {"slug": "billing-edge"}],
        }
        proc = self.run_harvest(
            envelope("mcp__plugin_gitian-kb_gitian__publish_topic", tool_response=response)
        )
        context = self.additional_context(proc)
        self.assertIn("auth-flow", context)
        self.assertIn("billing-edge", context)


class RepeatSuppression(MintFollowupTestCase):
    def test_identical_second_envelope_is_silent(self):
        env = envelope(
            "mcp__plugin_gitian-kb_gitian__publish_doc",
            tool_response=minted_response(["auth-flow"]),
        )
        first = self.run_harvest(env)
        self.additional_context(first)  # fires once

        second = self.run_harvest(env)
        self.assert_silent(second)

        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["mintPrompted"], ["auth-flow"])
        self.assertEqual(state["servers"][SERVER_KEY]["undescribedTopics"], ["auth-flow"])

    def test_new_slug_in_later_envelope_fires_again_naming_only_the_new_one(self):
        first = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=minted_response(["auth-flow", "billing-edge"]),
            )
        )
        self.additional_context(first)

        second = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=minted_response(["auth-flow", "billing-edge", "kb-search"]),
            )
        )
        context = self.additional_context(second)
        self.assertIn("kb-search", context)
        self.assertNotIn("auth-flow", context)
        self.assertNotIn("billing-edge", context)

        state = self.dump_state()
        self.assertEqual(
            sorted(state["sessions"]["sess-1"]["mintPrompted"]),
            ["auth-flow", "billing-edge", "kb-search"],
        )

    def test_every_slug_already_prompted_is_silent_even_with_a_reordered_list(self):
        self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=minted_response(["auth-flow", "billing-edge"]),
            )
        )
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=minted_response(["billing-edge", "auth-flow"]),
            )
        )
        self.assert_silent(proc)

    def test_separate_sessions_are_independent(self):
        first = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=minted_response(["auth-flow"]),
                session_id="sess-a",
            )
        )
        self.additional_context(first)

        second = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=minted_response(["auth-flow"]),
                session_id="sess-b",
            )
        )
        # A different session has never been prompted about "auth-flow" -- it must still fire.
        context = self.additional_context(second)
        self.assertIn("auth-flow", context)


class NoWarningIsSilent(MintFollowupTestCase):
    def test_envelope_without_the_warning_stays_silent(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response={"isError": False, "slug": "new-doc"},
            )
        )
        self.assert_silent(proc)
        state = self.dump_state()
        # The ordinary harvest side effect (T2) still creates the session (publish succeeded) --
        # mintPrompted/undescribedTopics simply stay at their empty defaults.
        self.assertEqual(state["sessions"]["sess-1"].get("mintPrompted"), [])
        self.assertNotIn("undescribedTopics", state["servers"].get(SERVER_KEY, {}))

    def test_non_gitian_call_mentioning_the_marker_stays_silent(self):
        proc = self.run_harvest(
            envelope(
                "Read",
                tool_input={"file_path": "/x"},
                tool_response={"organic_topics_minted": ["auth-flow"]},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))


class GarbageExtractsNothing(MintFollowupTestCase):
    def test_scalar_value_under_the_literal_member_extracts_nothing(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response={"isError": False, "organic_topics_minted": "not-a-list-or-dict"},
            )
        )
        self.assert_silent(proc)

    def test_unrecognized_nested_shape_under_the_literal_member_extracts_nothing(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response={
                    "isError": False,
                    "organic_topics_minted": {"unexpected": {"deeply": "nested"}, "slug": 123},
                },
            )
        )
        self.assert_silent(proc)

    def test_warning_code_matches_but_note_is_not_a_string_extracts_nothing(self):
        response = {
            "isError": False,
            "warnings": [{"code": "organic_topics_minted", "path": "topics", "note": None}],
        }
        proc = self.run_harvest(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_response=response)
        )
        self.assert_silent(proc)

    def test_note_with_no_recognizable_slug_tokens_extracts_nothing(self):
        response = {
            "isError": False,
            "warnings": [
                {
                    "code": "organic_topics_minted",
                    "path": "topics",
                    "note": "topics were auto-minted just now",  # no colon, no kebab tokens
                }
            ],
        }
        proc = self.run_harvest(
            envelope("mcp__plugin_gitian-kb_gitian__publish_doc", tool_response=response)
        )
        self.assert_silent(proc)

    def test_prose_mentioning_the_marker_word_extracts_nothing(self):
        # The raw-text gate matches (the marker string appears somewhere), but there is no
        # matching "code" object and no literal member anywhere in tool_response -- the
        # structured search must come up empty rather than false-positive on stray prose.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response={
                    "isError": False,
                    "message": "no organic_topics_minted warning this time",
                },
            )
        )
        self.assert_silent(proc)

    def test_malformed_whole_envelope_is_silent(self):
        proc = self.run_harvest(
            '{"tool_name": "mcp__plugin_gitian-kb_gitian__get", organic_topics_minted BROKEN'
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))


if __name__ == "__main__":
    unittest.main()
