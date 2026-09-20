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
        self.assertEqual(state["sessions"]["sess-1"]["lastSeenVocabRev"][SERVER_KEY]["home"], 5)

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
        self.assertEqual(state["sessions"]["sess-1"]["lastSeenVocabRev"][SERVER_KEY]["home"], 9)

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
        suffixes = (
            "get",
            "search",
            "list",
            "neighbors",
            "topic",
            "history",
            "file_intents",
        )
        for suffix in suffixes:
            proc = self.run_harvest(
                envelope("mcp__plugin_gitian-kb_gitian__%s" % suffix, tool_response={"ok": True})
            )
            self.assert_silent(proc)

        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["gitianReads"], len(suffixes))

    def test_read_resource_is_credited_by_uri_not_by_name(self):
        # [[kb-scribe-delegation]]: a plugin subagent has no ReadMcpResourceTool at all, so both
        # agents reach the vocabulary AND the static format docs through this one tool. Only the
        # vocabulary read is KB content, so the tool name alone must not earn the credit -- the
        # uri decides, which is what keeps a scribe's format-doc read from standing in for the
        # orientation sweep the parent's nudge is actually asking about.
        name = "mcp__plugin_gitian-kb_gitian__read_resource"
        self.assert_silent(
            self.run_harvest(
                envelope(name, tool_input={"uri": "gitian-kb://format/doc"},
                         tool_response={"vocab_rev": 3})
            )
        )
        self.assertEqual(self.dump_state()["sessions"]["sess-1"].get("gitianReads", 0), 0)

        self.assert_silent(
            self.run_harvest(
                envelope(name, tool_input={"uri": "gitian-kb://vocab"},
                         tool_response={"vocab_rev": 3})
            )
        )
        self.assertEqual(self.dump_state()["sessions"]["sess-1"]["gitianReads"], 1)

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
    VOCAB_REV_RE regex (which only ever matched an unescaped '"vocab_rev"') never fired against
    real server traffic; only harvest.py's own hand-built test envelopes (which put that field
    directly on tool_response, unnested) ever exercised it. These tests drive harvest.sh with the
    REALISTIC nested shape end to end. Publish success/failure is judged structurally rather than
    by regex at all now -- see PublishOutcome below."""

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
        self.assertEqual(state["sessions"]["sess-1"]["lastSeenVocabRev"][SERVER_KEY]["home"], 19)

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
        self.assertEqual(state["sessions"]["sess-1"]["lastSeenVocabRev"][SERVER_KEY]["home"], 42)

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


class DelegatedWriteSurface(HarvestTestCase):
    """[[kb-scribe-delegation]]: the write tools the SCRIBE actually reaches for. A subagent's
    MCP traffic fires these hooks under the parent's session id (proved by probe), so what the
    scribe does has to register here or the parent's Stop reminder nags about work that was
    published minutes ago."""

    def test_patch_doc_counts_as_a_publish(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__patch_doc",
                tool_input={"slug": "some-plan", "base_rev": 7},
                tool_response={"isError": False, "slug": "some-plan"},
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        session = state["sessions"]["sess-1"]
        self.assertEqual(session["publishes"], 1)
        self.assertEqual(session.get("gitianReads", 0), 0)
        server = state["servers"][SERVER_KEY]
        self.assertTrue(server["lastPublishAt"])
        self.assertEqual(server["lastPublishSlug"], "some-plan")
        # A patch is not an APPEND: the commit-nudge damper keys on lastAppendAt, and a
        # frontmatter-only patch is not the journal entry that damper is about.
        self.assertNotIn("lastAppendAt", server)

    def test_patch_memory_counts_as_a_publish(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__patch_memory",
                tool_response={"isError": False, "slug": "a-memory"},
            )
        )
        self.assert_silent(proc)
        self.assertEqual(self.dump_state()["sessions"]["sess-1"]["publishes"], 1)

    def test_failed_patch_harvests_nothing(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__patch_doc",
                tool_response={"isError": True, "error": "rev_conflict"},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_read_resource_of_the_vocab_uri_seeds_the_topic_cache(self):
        topics = [{"slug": "auth", "description": "Auth flows", "degree": 3}]
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__read_resource",
                tool_input={"uri": "gitian-kb://vocab"},
                tool_response={
                    "content": [{"type": "text", "text": json.dumps({"topics": topics, "vocab_rev": 51})}]
                },
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        server = state["servers"][SERVER_KEY]
        self.assertEqual(server["topics"], topics)
        self.assertEqual(server["vocabRev"], 51)
        self.assertTrue(server["vocabFetchedAt"])
        self.assertEqual(state["sessions"]["sess-1"]["gitianReads"], 1)

    def test_read_resource_of_a_format_doc_is_not_an_orientation_read(self):
        # A format doc is the publish-format INSTRUCTIONS -- static text, identical in every KB,
        # exposing no KB content at all. Crediting it as a `gitianReads` would let any scribe
        # dispatch silence the parent's orientation check with no sweep having happened, and the
        # orientation check is precisely "has anything in this session looked at the KB yet".
        # Parity note: on origin/main the other spelling of this read (ReadMcpResourceTool on a
        # `gitian-kb://format/*` uri) was NOT credited either -- READ_SUFFIXES held no
        # `read_resource` and "ReadMcpResourceTool" ends in none of its entries -- so this is
        # closing a regression, not narrowing established behavior.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__read_resource",
                tool_input={"uri": "gitian-kb://format/doc"},
                tool_response=self._resource_response(
                    "gitian-kb://format/doc", "text/markdown", "# Doc format", vocab_rev=12
                ),
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"].get("gitianReads", 0), 0)
        server = state["servers"][SERVER_KEY]
        # The envelope is still mined for everything it legitimately carries.
        self.assertEqual(server["vocabRev"], 12)
        self.assertNotIn("topics", server)

    def _resource_response(self, uri, mime_type, text, vocab_rev=None):
        """The REAL `read_resource` tool envelope (mcp-server.ts: `jsonResult({uri, mimeType,
        text})`, then `stampVocabRev`). The resource's own payload is a JSON STRING nested under
        `text` inside the content block's own JSON string -- two levels of encoding, not one,
        which is why a reader that decodes a single level finds no `topics` key at all."""
        payload = {"uri": uri, "mimeType": mime_type, "text": text}
        if vocab_rev is not None:
            payload["vocab_rev"] = vocab_rev
        return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}

    def test_read_resource_vocab_envelope_seeds_the_topic_cache(self):
        # Bundle S's `read_resource` is the path a SUBAGENT must use (no ReadMcpResourceTool in a
        # plugin subagent's registry), so this is the shape the vocab snapshot will actually
        # arrive in from here on.
        topics = [{"slug": "auth", "description": "Auth flows", "degree": 3}]
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__read_resource",
                tool_input={"uri": "gitian-kb://vocab"},
                tool_response=self._resource_response(
                    "gitian-kb://vocab",
                    "application/json",
                    json.dumps({"topics": topics}),
                    vocab_rev=77,
                ),
            )
        )
        self.assert_silent(proc)

        state = self.dump_state()
        server = state["servers"][SERVER_KEY]
        self.assertEqual(server["topics"], topics)
        self.assertEqual(server["vocabRev"], 77)
        self.assertTrue(server["vocabFetchedAt"])
        self.assertEqual(state["sessions"]["sess-1"]["gitianReads"], 1)

    def test_read_resource_vocab_envelope_records_undescribed_topics(self):
        topics = [
            {"slug": "auth", "description": "Auth flows", "degree": 3},
            {"slug": "stub-topic", "description": "", "degree": 1},
        ]
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__read_resource",
                tool_input={"uri": "gitian-kb://vocab"},
                tool_response=self._resource_response(
                    "gitian-kb://vocab", "application/json", json.dumps({"topics": topics})
                ),
            )
        )
        self.assert_silent(proc)
        server = self.dump_state()["servers"][SERVER_KEY]
        self.assertEqual(server["undescribedTopics"], ["stub-topic"])

    def test_read_resource_vocab_envelope_with_unparsable_nested_text_harvests_no_topics(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__read_resource",
                tool_input={"uri": "gitian-kb://vocab"},
                tool_response=self._resource_response(
                    "gitian-kb://vocab", "application/json", "not json at all {"
                ),
            )
        )
        self.assert_silent(proc)
        self.assertNotIn("topics", self.dump_state().get("servers", {}).get(SERVER_KEY, {}))


class PublishOutcome(HarvestTestCase):
    """A failed write must never be counted as a publish. The Stop reminder short-circuits on
    `sessions.<sid>.publishes > 0`, so one miscounted `rev_conflict` silences the nudge for the
    whole epoch -- exactly the case where the work did NOT reach the KB and the reminder is most
    needed. Success is therefore modelled POSITIVELY from the server's real envelopes
    (mcp-server.ts/repository.ts): a success carries a `slug` and, on the publish/patch tails, a
    numeric `rev`; every error is `errResult({error: "<code>", ...})`, a STRING `error`."""

    def _nested(self, payload, is_error=False):
        return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": is_error}

    def _success_payload(self, slug="some-plan", rev=8, **extra):
        """repository.ts::PublishSuccess as the wire actually carries it."""
        payload = {
            "slug": slug,
            "primitive": "doc",
            "rev": rev,
            "url": "/kb/home/doc/%s" % slug,
            "body_length": 1234,
            "body_hash": "deadbeef",
            "vocab_rev": 19,
        }
        payload.update(extra)
        return payload

    def test_a_real_success_envelope_counts(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=self._nested(self._success_payload()),
            )
        )
        self.assert_silent(proc)
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 1)
        self.assertEqual(state["servers"][SERVER_KEY]["lastPublishSlug"], "some-plan")

    def test_rev_conflict_does_not_count_as_a_publish(self):
        # THE trap: a rev_conflict carries the item's own `slug` (repository.ts::revConflictError)
        # and no top-level marker the old raw-substring checks looked for, so slug-or-nothing
        # detection read it as a successful publish of that very slug.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__patch_doc",
                tool_input={"slug": "some-plan", "base_rev": 6},
                tool_response=self._nested(
                    {
                        "error": "rev_conflict",
                        "slug": "some-plan",
                        "base_rev": 6,
                        "head_rev": 8,
                        "head": {"author": "scribe", "author_login": "arrayofone"},
                        "frontmatter_changed": ["status"],
                        "body_diff": "@@ -1 +1 @@",
                        "omitted_reason": None,
                        "message": "'some-plan' moved from rev 6 to rev 8 since you read it",
                    },
                    is_error=True,
                ),
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_rev_conflict_is_detected_structurally_even_without_the_transport_flag(self):
        # Whether the harness forwards the MCP envelope's own `isError` into `tool_response` is
        # outside this hook's control; the decoded `error` string is not.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=self._nested(
                    {"error": "rev_conflict", "slug": "some-plan", "head_rev": 8}, is_error=False
                ),
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_base_rev_required_does_not_count_as_a_publish(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=self._nested(
                    {
                        "error": "base_rev_required",
                        "slug": "some-plan",
                        "message": "'some-plan' already exists; read it and retry",
                    },
                    is_error=True,
                ),
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_edit_no_match_does_not_count_as_a_publish(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__patch_doc",
                tool_response=self._nested(
                    {"error": "edit_no_match", "slug": "some-plan", "message": "no match"},
                    is_error=True,
                ),
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_a_no_op_republish_does_not_count_as_a_publish(self):
        # The server reports a write that stored nothing as `unchanged: true` on an otherwise
        # ordinary success envelope (repository.ts: the `noop` plan). Nothing reached the KB this
        # session, so it must not silence the Stop reminder -- and counting it would hand an agent
        # a way to buy credit by re-sending an old body verbatim. The vocab_rev it carries is
        # still harvested; only the publish counter and the publish timestamps abstain.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=self._nested(self._success_payload(unchanged=True)),
            )
        )
        self.assert_silent(proc)
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 0)
        server = state["servers"][SERVER_KEY]
        self.assertNotIn("lastPublishAt", server)
        self.assertNotIn("lastPublishSlug", server)
        self.assertEqual(server["vocabRev"], 19)

    def test_a_publish_response_with_no_recognizable_envelope_is_not_counted(self):
        # No positive evidence of a write is not evidence of one. Silence beats a false credit,
        # because the credit is what suppresses the reminder.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response={"content": [{"type": "text", "text": "server said something"}]},
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_an_ok_false_envelope_is_a_failure(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=self._nested({"ok": False, "slug": "some-plan", "rev": 3}),
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_a_non_numeric_rev_is_not_a_success_envelope(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_response=self._nested({"slug": "some-plan", "rev": "eight"}),
            )
        )
        self.assert_silent(proc)
        self.assertFalse(os.path.exists(self.state_file))

    def test_publish_topic_success_counts_without_a_rev(self):
        # publish_topic's success envelope is `{slug, state, degree}` -- a real write with no
        # revision number at all, so `rev` has to be optional-when-absent rather than required.
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__publish_topic",
                tool_response=self._nested(
                    {"slug": "kb-discipline", "state": "organic", "degree": 4, "vocab_rev": 20}
                ),
            )
        )
        self.assert_silent(proc)
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 1)
        self.assertEqual(state["servers"][SERVER_KEY]["lastPublishSlug"], "kb-discipline")

    def test_append_entry_success_still_sets_last_append_at(self):
        proc = self.run_harvest(
            envelope(
                "mcp__plugin_gitian-kb_gitian__append_entry",
                tool_response=self._nested(
                    {
                        "slug": "journal-2026-09-17",
                        "action": "appended",
                        "rev": 4,
                        "url": "/kb/home/entry/journal-2026-09-17",
                        "body_length": 900,
                        "body_hash": "cafe",
                    }
                ),
            )
        )
        self.assert_silent(proc)
        server = self.dump_state()["servers"][SERVER_KEY]
        self.assertTrue(server["lastAppendAt"])
        self.assertEqual(server["lastPublishSlug"], "journal-2026-09-17")


if __name__ == "__main__":
    unittest.main()
