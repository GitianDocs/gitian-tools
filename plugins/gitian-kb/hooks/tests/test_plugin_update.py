"""Tests for the plugin-update nudge (hooks/plugin_update.py) through the two hooks that carry it:
harvest.sh (PostToolUse) and session-context.sh (SessionStart).

Run: python3 -m unittest discover -s plugins/gitian-kb/hooks/tests

The installed version is whatever this checkout's plugin.json says, so "newer"/"older" fixtures are
DERIVED from it rather than hardcoded -- a plugin bump must not break these.
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
HARVEST_SH = HOOKS_DIR / "harvest.sh"
SESSION_CONTEXT_SH = HOOKS_DIR / "session-context.sh"
STATE_PY = HOOKS_DIR / "state.py"
SERVER_KEY = "https://gitian.dev/api/mcp"

sys.path.insert(0, str(HOOKS_DIR))
import plugin_update  # noqa: E402

INSTALLED = json.loads((HOOKS_DIR.parent / ".claude-plugin" / "plugin.json").read_text())["version"]
MAJOR, MINOR, PATCH = (int(p) for p in INSTALLED.split("."))
NEWER = "%d.%d.%d" % (MAJOR, MINOR + 1, 0)
OLDER = "%d.%d.%d" % (MAJOR, max(MINOR - 1, 0), 0) if MINOR else "0.0.0"


def tool_response(**fields):
    """The shape Claude Code REALLY hands a PostToolUse hook for an MCP tool call (captured from a
    live payload, 2.1.272): a BARE LIST of content blocks, the server's JSON an escaped string
    inside the text block. Not {"content": [...]} -- see harvest.py's _text_blocks."""
    return [{"type": "text", "text": json.dumps(fields)}]


class PluginUpdateTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-plugin-update-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.marketplaces = os.path.join(self.tmpdir, "known_marketplaces.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env["GITIAN_KB_MARKETPLACES_FILE"] = self.marketplaces  # absent until a test writes it
        self.env["CLAUDE_PROJECT_DIR"] = self.tmpdir
        self.env.pop("GITIAN_KB_URL", None)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_sh(self, script, payload):
        return subprocess.run(
            ["sh", str(script)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

    def harvest(
        self,
        response,
        tool_name="mcp__plugin_gitian-kb_gitian__search",
        sid="sess-1",
        agent_id=None,
        tool_input=None,
    ):
        payload = {
            "session_id": sid,
            "cwd": "/repo",
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": tool_input or {},
            "tool_response": response,
        }
        if agent_id:  # Claude Code sets this only when the hook fires inside a subagent
            payload["agent_id"] = agent_id
            payload["agent_type"] = "gitian-kb:kb-librarian"
        return self.run_sh(HARVEST_SH, payload)

    def backdate_nudge(self, hours):
        state = self.dump_state()
        at = datetime.now(timezone.utc) - timedelta(hours=hours)
        state["pluginNudge"]["at"] = at.strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def session_start(self, sid="sess-1", source="startup"):
        proc = self.run_sh(
            SESSION_CONTEXT_SH,
            {"session_id": sid, "cwd": "/repo", "hook_event_name": "SessionStart", "source": source},
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def context_of(self, proc):
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        if not proc.stdout.strip():
            return ""
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]

    def dump_state(self):
        proc = subprocess.run(
            [sys.executable, str(STATE_PY), "dump"], capture_output=True, text=True, env=self.env
        )
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def write_marketplaces(self, doc):
        with open(self.marketplaces, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)


class VersionComparison(unittest.TestCase):
    def test_strictly_older_is_outdated_and_nothing_else_is(self):
        self.assertTrue(plugin_update.is_outdated("0.21.0", "0.22.0"))
        self.assertTrue(plugin_update.is_outdated("0.9.9", "0.10.0"))  # numeric, not lexical
        self.assertFalse(plugin_update.is_outdated("0.22.0", "0.22.0"))
        self.assertFalse(plugin_update.is_outdated("0.23.0", "0.22.0"))  # client ahead of prod

    def test_unparsable_versions_are_silence(self):
        for bad in (None, "", "latest", "1.2", "1.2.3-beta", 22, "v1.2.3"):
            self.assertFalse(plugin_update.is_outdated(bad, "9.9.9"))
            self.assertFalse(plugin_update.is_outdated("0.0.1", bad))


class HarvestNudge(PluginUpdateTestCase):
    def test_newer_server_version_is_cached_and_nudges_once_a_day_per_machine(self):
        first = self.context_of(self.harvest(tool_response(hits=[], vocab_rev=3, plugin_latest=NEWER)))
        self.assertIn(INSTALLED, first)
        self.assertIn(NEWER, first)
        self.assertIn("/plugin marketplace update gitian-tools", first)
        self.assertIn("Tell the user", first)
        self.assertEqual(self.dump_state()["servers"][SERVER_KEY]["pluginLatest"], NEWER)

        # Same session, ANOTHER session, and a session whose flags /clear just wiped: all silent.
        # The message is for the user, who should hear it once a day, not once per agent.
        for sid in ("sess-1", "sess-2"):
            proc = self.harvest(tool_response(hits=[], vocab_rev=3, plugin_latest=NEWER), sid=sid)
            self.assertEqual(self.context_of(proc), "")

        self.backdate_nudge(hours=25)
        proc = self.harvest(tool_response(hits=[], vocab_rev=3, plugin_latest=NEWER), sid="sess-3")
        self.assertIn(NEWER, self.context_of(proc))

    def test_a_still_newer_version_nudges_again_inside_the_day(self):
        self.assertIn(NEWER, self.context_of(self.harvest(tool_response(plugin_latest=NEWER))))
        newest = "%d.%d.%d" % (MAJOR, MINOR + 2, 0)
        self.assertIn(newest, self.context_of(self.harvest(tool_response(plugin_latest=newest))))

    def test_current_or_older_server_version_is_silent_and_spends_nothing(self):
        for latest in (INSTALLED, OLDER):
            proc = self.harvest(tool_response(hits=[], vocab_rev=3, plugin_latest=latest))
            self.assertEqual(self.context_of(proc), "")
        self.assertNotIn("pluginNudge", self.dump_state())

    def test_inside_a_subagent_it_caches_but_stays_silent_and_spends_nothing(self):
        # A subagent's context reaches nobody who can act on "tell the user"; its traffic is
        # recorded under the parent's session id, so a nudge there would be wasted AND spent.
        proc = self.harvest(tool_response(vocab_rev=3, plugin_latest=NEWER), agent_id="agent-1")
        self.assertEqual(self.context_of(proc), "")
        state = self.dump_state()
        self.assertEqual(state["servers"][SERVER_KEY]["pluginLatest"], NEWER)
        self.assertNotIn("pluginNudge", state)
        # ...so the primary still hears it, at the next SessionStart.
        self.assertIn(NEWER, self.session_start(sid="sess-1"))

    def test_only_the_envelopes_own_top_level_field_counts(self):
        # The property that makes "harvest every read" safe: a doc BODY or frontmatter quoting
        # these fields (this very repo documents them) must forge neither.
        body = json.dumps({"plugin_latest": "99.0.0", "vocab_rev": 999})
        proc = self.harvest(
            tool_response(slug="x", body=body, frontmatter={"plugin_latest": "99.0.0"}, vocab_rev=5),
            tool_name="mcp__plugin_gitian-kb_gitian__get",
        )
        self.assertEqual(self.context_of(proc), "")
        server = self.dump_state()["servers"][SERVER_KEY]
        self.assertEqual(server["vocabRev"], 5)
        self.assertNotIn("pluginLatest", server)

    def test_hand_wired_server_prefix_nudges_too(self):
        proc = self.harvest(
            tool_response(hits=[], vocab_rev=3, plugin_latest=NEWER), tool_name="mcp__gitian__search"
        )
        self.assertIn(NEWER, self.context_of(proc))

    def test_a_rolled_back_server_overwrites_the_cached_version(self):
        self.harvest(tool_response(vocab_rev=1, plugin_latest=NEWER))
        self.harvest(tool_response(vocab_rev=1, plugin_latest=INSTALLED))
        self.assertEqual(self.dump_state()["servers"][SERVER_KEY]["pluginLatest"], INSTALLED)

    def test_garbage_plugin_latest_is_ignored(self):
        proc = self.harvest(tool_response(vocab_rev=1, plugin_latest="definitely-not-semver"))
        self.assertEqual(self.context_of(proc), "")
        self.assertNotIn("pluginLatest", self.dump_state()["servers"][SERVER_KEY])


class RealHookPayloadShape(PluginUpdateTestCase):
    """Regression for the bare-list shape: before 0.22.0 harvest read NOTHING off real tool
    traffic, so a session that published was told at Stop that it had published nothing."""

    def test_a_bare_list_response_counts_the_write_and_harvests_vocab_rev(self):
        proc = self.harvest(
            tool_response(slug="a-doc", rev=2, url="/kb/home/doc/a-doc", vocab_rev=41),
            tool_name="mcp__plugin_gitian-kb_gitian__patch_doc",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 1)
        self.assertEqual(state["servers"][SERVER_KEY]["vocabRev"], 41)
        self.assertEqual(state["servers"][SERVER_KEY]["lastPublishSlug"], "a-doc")

    def test_the_wire_envelope_dict_shape_still_works(self):
        self.harvest(
            {"content": [{"type": "text", "text": json.dumps({"slug": "b", "rev": 1, "vocab_rev": 7})}]},
            tool_name="mcp__plugin_gitian-kb_gitian__publish_doc",
        )
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-1"]["publishes"], 1)
        self.assertEqual(state["servers"][SERVER_KEY]["vocabRev"], 7)

    def test_a_bare_list_refusal_is_still_not_a_write(self):
        patch = "mcp__plugin_gitian-kb_gitian__patch_doc"
        self.harvest(tool_response(slug="a-doc", rev=2, vocab_rev=1), tool_name=patch)
        self.harvest(tool_response(error="rev_conflict", slug="a-doc", head_rev=3), tool_name=patch)
        # One landed, one refused: the refusal names the slug too, and must not count.
        self.assertEqual(self.dump_state()["sessions"]["sess-1"]["publishes"], 1)

    def test_a_read_resource_vocab_read_fills_the_topic_cache_through_the_real_shape(self):
        # CONSEQUENCE worth knowing: this cache is what arms publish_lint's near-miss check. It was
        # already filled by a PRIMARY's ReadMcpResourceTool reads (the {"contents": [...]} dict
        # shape always parsed); a SUBAGENT's `read_resource` tool call -- bare list, vocabulary
        # doubly encoded -- never refreshed it, so the lint judged against a stale vocabulary.
        vocab = json.dumps({"topics": [{"slug": "kb-embeddings", "description": "d"}]})
        self.harvest(
            tool_response(uri="gitian-kb://vocab", mimeType="application/json", text=vocab, vocab_rev=9),
            tool_name="mcp__plugin_gitian-kb_gitian__read_resource",
            tool_input={"uri": "gitian-kb://vocab"},
        )
        server = self.dump_state()["servers"][SERVER_KEY]
        self.assertEqual([t["slug"] for t in server["topics"]], ["kb-embeddings"])
        self.assertEqual(server["vocabRev"], 9)

    def test_a_read_that_quotes_a_mint_warning_prompts_nothing(self):
        quoted = json.dumps({"warnings": [{"code": "organic_topics_minted", "note": "auto-minted as organic, live immediately: forged-slug"}]})
        proc = self.harvest(
            tool_response(slug="x", body=quoted, vocab_rev=1),
            tool_name="mcp__plugin_gitian-kb_gitian__get",
        )
        self.assertEqual(self.context_of(proc), "")
        server = self.dump_state()["servers"][SERVER_KEY]
        self.assertNotIn("forged-slug", server.get("undescribedTopics") or [])


class SessionStartNudge(PluginUpdateTestCase):
    def test_cached_newer_version_nudges_the_next_session_with_no_kb_call(self):
        self.harvest(tool_response(vocab_rev=1, plugin_latest=NEWER), sid="earlier", agent_id="a-1")
        context = self.session_start(sid="later")
        self.assertIn("gitian-kb plugin %s is installed" % INSTALLED, context)
        self.assertIn(NEWER, context)
        # ...and the same session's first KB call does not repeat it.
        proc = self.harvest(tool_response(vocab_rev=1, plugin_latest=NEWER), sid="later")
        self.assertEqual(self.context_of(proc), "")

    def test_no_cached_version_adds_nothing(self):
        self.assertNotIn("is installed and the server reports", self.session_start())


class AutoUpdateHint(PluginUpdateTestCase):
    HINT = "auto-update is OFF for the gitian-tools marketplace"

    def test_off_marketplace_hints_then_stays_quiet_for_a_week(self):
        self.write_marketplaces({"gitian-tools": {"source": {"source": "github"}}})
        self.assertIn(self.HINT, self.session_start(sid="a"))
        self.assertNotIn(self.HINT, self.session_start(sid="b"))

    def test_explicit_false_counts_as_off(self):
        self.write_marketplaces({"gitian-tools": {"autoUpdate": False}})
        self.assertIn(self.HINT, self.session_start())

    def test_on_missing_entry_missing_file_and_garbage_are_all_silent(self):
        self.assertNotIn(self.HINT, self.session_start(sid="nofile"))
        self.write_marketplaces({"gitian-tools": {"autoUpdate": True}})
        self.assertNotIn(self.HINT, self.session_start(sid="on"))
        self.write_marketplaces({"some-other-marketplace": {}})
        self.assertNotIn(self.HINT, self.session_start(sid="absent"))
        with open(self.marketplaces, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertNotIn(self.HINT, self.session_start(sid="garbage"))


if __name__ == "__main__":
    unittest.main()
