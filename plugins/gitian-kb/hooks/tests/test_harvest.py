#!/usr/bin/env python3
"""Unit tests for harvest.py, the gitian-kb nudge layer's PostToolUse harvester.

Drives it end to end via `sh harvest.sh` (matching how hooks.json actually invokes it), with
GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_harvest.py
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


class HarvestTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-harvest-test-")
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


class GuardClause(HarvestTestCase):
    def test_non_gitian_tool_is_silent_and_untouched(self):
        proc = self.run_harvest(envelope("Read", tool_input={"file_path": "/x"}))
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_empty_stdin_is_silent(self):
        proc = self.run_harvest("")
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_garbage_stdin_is_silent(self):
        proc = self.run_harvest("{not valid json ][ at all")
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_read_mcp_resource_tool_non_gitian_uri_is_silent(self):
        proc = self.run_harvest(
            envelope("ReadMcpResourceTool", tool_input={"uri": "other://thing"})
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_read_mcp_resource_tool_gitian_uri_but_no_harvestable_fields_is_silent(self):
        # Guard passes (gitian-kb:// uri) but it's not the vocab resource and the response has
        # no vocab_rev/slug -- nothing to harvest, so no write should happen at all.
        proc = self.run_harvest(
            envelope(
                "ReadMcpResourceTool",
                tool_input={"uri": "gitian-kb://format/doc"},
                tool_response={"contents": [{"uri": "gitian-kb://format/doc", "text": "# Doc format"}]},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_corrupt_state_file_is_survived_and_rebuilt(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")

        proc = self.run_harvest(envelope("mcp__plugin_gitian-kb_gitian__get", tool_input={"slug": "x"}))
        self.assert_silent(proc)

        state = self.dump_state()
        self.assertEqual(state.get("schemaVersion"), 1)
        self.assertEqual(state["sessions"]["sess-1"]["gitianReads"], 1)


class VocabRevCapture(HarvestTestCase):
    def test_captured_from_envelope_and_stored_on_server_and_session(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__get",
                tool_input={"slug": "x"},
                tool_response={"isError": False, "vocab_rev": 5, "slug": "x"},
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        self.assertEqual(state["servers"][SERVER_KEY]["vocabRev"], 5)
        self.assertEqual(state["sessions"]["sess-1"]["lastSeenVocabRev"], 5)

    def test_max_wins_on_regression(self):
        self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__get",
                tool_response={"vocab_rev": 9},
            )
        )
        self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__get",
                tool_response={"vocab_rev": 3},
            )
        )

        state = self.dump_state()
        self.assertEqual(state["servers"][SERVER_KEY]["vocabRev"], 9)
        self.assertEqual(state["sessions"]["sess-1"]["lastSeenVocabRev"], 9)

    def test_multiple_occurrences_take_the_max(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__search",
                tool_response={"vocab_rev": 2, "extra": {"vocab_rev": 7}},
            )
        )
        self.assert_silent(proc)
        state = self.dump_state()
        self.assertEqual(state["servers"][SERVER_KEY]["vocabRev"], 7)


class VocabTopicList(HarvestTestCase):
    def _vocab_envelope(self, topics):
        text = json.dumps({"topics": topics})
        return envelope(
            "ReadMcpResourceTool",
            tool_input={"uri": "gitian-kb://vocab"},
            tool_response={"contents": [{"uri": "gitian-kb://vocab", "text": text}]},
        )

    def test_topics_stored_with_undescribed_detection(self):
        topics = [
            {"slug": "auth", "description": "Authentication flows", "degree": 4},
            {"slug": "billing", "description": "", "degree": 2},
            {"slug": "kb", "degree": 1},  # description missing entirely
        ]
        proc = self.run_harvest(self._vocab_envelope(topics))
        self.assert_silent(proc)

        state = self.dump_state()
        server = state["servers"][SERVER_KEY]
        self.assertEqual(
            server["topics"],
            [
                {"slug": "auth", "description": "Authentication flows", "degree": 4},
                {"slug": "billing", "description": "", "degree": 2},
                {"slug": "kb", "description": "", "degree": 1},
            ],
        )
        self.assertEqual(sorted(server["undescribedTopics"]), ["billing", "kb"])
        self.assertIn("vocabFetchedAt", server)

        # The vocab resource read also counts as a gitianReads increment.
        self.assertEqual(state["sessions"]["sess-1"]["gitianReads"], 1)

    def test_capped_at_200(self):
        topics = [{"slug": "t%03d" % i, "description": "d", "degree": i} for i in range(250)]
        proc = self.run_harvest(self._vocab_envelope(topics))
        self.assert_silent(proc)

        state = self.dump_state()
        stored = state["servers"][SERVER_KEY]["topics"]
        self.assertEqual(len(stored), 200)
        self.assertEqual([t["slug"] for t in stored], ["t%03d" % i for i in range(200)])

    def test_unparsable_content_harvests_nothing(self):
        proc = self.run_harvest(
            envelope(
                "ReadMcpResourceTool",
                tool_input={"uri": "gitian-kb://vocab"},
                tool_response={"contents": [{"uri": "gitian-kb://vocab", "text": "not json at all"}]},
            )
        )
        self.assert_silent(proc)
        state = self.dump_state()
        # Guard passed and it IS a read (vocab resource), so gitianReads still increments --
        # only the topics/undescribedTopics/vocabFetchedAt fields are skipped.
        self.assertEqual(state["sessions"]["sess-1"]["gitianReads"], 1)
        self.assertNotIn("topics", state["servers"].get(SERVER_KEY, {}))


class Reads(HarvestTestCase):
    def test_increments_on_get_and_search_but_not_on_publish(self):
        self.run_harvest(envelope("mcp__plugin_gitian-kb_gitian__get", tool_response={"ok": True}))
        self.run_harvest(envelope("mcp__plugin_gitian-kb_gitian__search", tool_response={"ok": True}))
        self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response={"isError": False, "slug": "new-doc"},
            )
        )

        state = self.dump_state()
        session = state["sessions"]["sess-1"]
        self.assertEqual(session["gitianReads"], 2)
        self.assertEqual(session["publishes"], 1)

    def test_all_documented_read_suffixes_increment(self):
        suffixes = ("get", "search", "list", "neighbors", "topic", "history", "file_intents")
        for suffix in suffixes:
            proc = self.run_harvest(
                envelope("mcp__plugin_gitian-kb_gitian__%s" % suffix, tool_response={"ok": True})
            )
            self.assert_silent(proc)

        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["gitianReads"], len(suffixes))

    def test_non_read_non_publish_gitian_call_does_not_increment_reads(self):
        proc = self.run_harvest(
            envelope("mcp__plugin_gitian-kb_gitian__retract_item", tool_response={"ok": True})
        )
        self.assert_silent(proc)
        state = self.dump_state()
        # retract_item matches neither a read suffix nor a publish marker, and the response has
        # no vocab_rev -- nothing at all should be harvested.
        self.assertFalse(os.path.exists(self.state_file))

    def test_retract_topic_does_not_increment_reads(self):
        # "retract_topic" ends in the "topic" read-suffix but is neither a read nor a publish --
        # it must not be miscounted as an orientation read (regression for the "topic" suffix
        # also matching publish_topic/retract_topic).
        proc = self.run_harvest(
            envelope("mcp__plugin_gitian-kb_gitian__retract_topic", tool_response={"ok": True})
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_publish_topic_increments_publishes_not_reads(self):
        # "publish_topic" also ends in the "topic" read-suffix; a publish call must win precedence
        # over the read-suffix match so it isn't double-counted as both a read and a publish.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_topic",
                tool_response={"isError": False, "slug": "new-topic"},
            )
        )
        self.assert_silent(proc)
        state = self.dump_state()
        session = state["sessions"]["sess-1"]
        self.assertEqual(session.get("gitianReads", 0), 0)
        self.assertEqual(session["publishes"], 1)


class PublishSuccess(HarvestTestCase):
    def test_updates_last_publish_at_and_slug_and_increments_publishes(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response={"isError": False, "slug": "onboarding-guide"},
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        server = state["servers"][SERVER_KEY]
        self.assertIsNotNone(server["lastPublishAt"])
        self.assertEqual(server["lastPublishSlug"], "onboarding-guide")
        self.assertNotIn("lastAppendAt", server)  # publish_doc is not an append-shaped tool
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 1)

    def test_append_entry_also_sets_last_append_at(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__append_entry",
                tool_response={"isError": False, "slug": "journal-2026-07-18"},
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        server = state["servers"][SERVER_KEY]
        self.assertIsNotNone(server["lastPublishAt"])
        self.assertIsNotNone(server["lastAppendAt"])
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 1)

    def test_publish_entry_also_sets_last_append_at(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_entry",
                tool_response={"isError": False, "slug": "entry-1"},
            )
        )
        self.assert_silent(proc)
        state = self.dump_state()
        self.assertIsNotNone(state["servers"][SERVER_KEY]["lastAppendAt"])

    def test_failed_publish_is_error_true_harvests_nothing(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response={"isError": True, "message": "boom"},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_failed_publish_validation_failed_harvests_nothing(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_memory",
                tool_response={"isError": False, "error": "validation_failed: missing summary"},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_success_response_not_poisoned_by_validation_failed_word_in_tool_input(self):
        # Regression: success/slug detection must scope to tool_response only. A compliant call
        # whose tool_input body merely *mentions* "validation_failed" (e.g. documenting how
        # linting works) must still be harvested as a success -- it must not be misread as a
        # failed publish just because that substring appears somewhere in the raw envelope.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__append_entry",
                tool_input={"body": "This entry documents how validation_failed errors are linted."},
                tool_response={"isError": False, "slug": "journal-2026-07-18"},
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        server = state["servers"][SERVER_KEY]
        self.assertIsNotNone(server["lastPublishAt"])
        self.assertIsNotNone(server["lastAppendAt"])
        self.assertEqual(server["lastPublishSlug"], "journal-2026-07-18")
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 1)

    def test_success_response_not_poisoned_by_is_error_true_in_tool_input(self):
        # Same scoping regression, with the other poison marker ('"isError": true') appearing as
        # real JSON structure (not an escaped string) inside tool_input rather than tool_response
        # -- e.g. a doc body embedding a worked example of a failing call.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"exampleBadCall": {"isError": True, "message": "boom"}},
                tool_response={"isError": False, "slug": "linting-notes"},
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        server = state["servers"][SERVER_KEY]
        self.assertIsNotNone(server["lastPublishAt"])
        self.assertEqual(server["lastPublishSlug"], "linting-notes")
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 1)


class NestedMcpEnvelope(HarvestTestCase):
    """Regression for the CRITICAL nested-envelope decode fix. A REAL MCP tool response nests the
    server's actual JSON payload as an ESCAPED STRING inside a content block --
    {"content": [{"type": "text", "text": "{\\"vocab_rev\\": 19, ...}"}]} -- so the raw
    VOCAB_REV_RE/SLUG_RE regexes (which only ever matched an unescaped '"vocab_rev"'/'"slug"')
    never fired against real server traffic; only harvest.py's own hand-built test envelopes
    (which put those fields directly on tool_response, unnested) ever exercised them. These tests
    drive harvest.sh with the REALISTIC nested shape end to end."""

    def _nested_response(self, payload, is_error=False):
        return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}

    def test_vocab_rev_harvested_from_nested_publish_envelope(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=self._nested_response({"slug": "onboarding-guide", "vocab_rev": 19}),
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        self.assertEqual(state["servers"][SERVER_KEY]["vocabRev"], 19)
        self.assertEqual(state["sessions"]["sess-1"]["lastSeenVocabRev"], 19)

    def test_vocab_rev_and_topics_harvested_from_nested_vocab_resource_read(self):
        topics = [{"slug": "auth", "description": "Auth flows", "degree": 3}]
        proc = self.run_harvest(
            envelope(
                "ReadMcpResourceTool",
                tool_input={"uri": "gitian-kb://vocab"},
                tool_response=self._nested_response({"topics": topics, "vocab_rev": 42}),
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        server = state["servers"][SERVER_KEY]
        self.assertEqual(server["vocabRev"], 42)
        self.assertEqual(server["topics"], topics)
        self.assertEqual(state["sessions"]["sess-1"]["lastSeenVocabRev"], 42)

    def test_last_publish_slug_captured_from_nested_publish_success(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_memory",
                tool_response=self._nested_response({"slug": "nested-slug-fact"}),
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        server = state["servers"][SERVER_KEY]
        self.assertEqual(server["lastPublishSlug"], "nested-slug-fact")
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 1)

    def test_nested_validation_failed_harvests_no_publish_success(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=self._nested_response(
                    {"error": "validation_failed", "message": "bad input"}
                ),
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_nested_decoded_is_error_true_harvests_no_publish_success(self):
        # The outer MCP envelope itself succeeded at the transport level ("isError": false on the
        # tool_response) but the DECODED inner payload carries the real isError:true failure --
        # exactly the shape the old '"isError": true' raw-substring check could never see (the
        # escaped '\"isError\": true' text doesn't contain that literal substring).
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=self._nested_response(
                    {"error": "internal", "isError": True}, is_error=False
                ),
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))


if __name__ == "__main__":
    unittest.main()
