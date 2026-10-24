#!/usr/bin/env python3
"""Unit tests for brief_watch.py, the PostToolUse hook that records a kb-scribe brief as a
checkpoint of the gitian-kb nudge layer's activity ledger.

Drives it end to end via `sh brief-watch.sh` (matching how hooks.json actually invokes it), with
GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other. State is seeded by writing a
v1-shaped JSON document directly (the exact ledger a test needs is clearer as a literal dict than
composed through hooks), and read back through state.py's own `dump`.

The Agent tool's `tool_response` shape is unverified, so the agent id is exercised in every shape
it could plausibly take: a dict with an `agentId` key, a bare list of text blocks carrying
`agentId: a1b2...`, and a plain string.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_brief_watch.py
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
BRIEF_WATCH_SH = HOOKS_DIR / "brief-watch.sh"
STATE_PY = HOOKS_DIR / "state.py"

sys.path.insert(0, str(HOOKS_DIR))

import ledger as ledger_mod  # noqa: E402  (the module the hook records into lives one directory up)

SID = "sess-1"
AGENT_ID = "a1b2c3d4e5f6a7b8"
OTHER_ID = "z9y8x7w6v5u4t3s2"


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**delta):
    return _iso(datetime.now(timezone.utc) - timedelta(**delta))


def dispatch(subagent_type="kb-scribe", tool_response=None, tool_name="Agent", **extra):
    """An Agent (older name: Task) PostToolUse envelope."""
    payload = {
        "session_id": SID,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": {
            "subagent_type": subagent_type,
            "description": "brief the scribe",
            "prompt": "Write up what happened.",
            "run_in_background": True,
        },
        "tool_response": tool_response if tool_response is not None else {"agentId": AGENT_ID},
    }
    payload.update(extra)
    return payload


def message(to, **extra):
    """A SendMessage PostToolUse envelope."""
    payload = {
        "session_id": SID,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PostToolUse",
        "tool_name": "SendMessage",
        "tool_input": {"to": to, "message": "one more thing to record"},
        "tool_response": {"success": True},
    }
    payload.update(extra)
    return payload


class BriefWatchTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-brief-watch-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env.pop("GITIAN_KB_URL", None)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def run_hook(self, payload):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(BRIEF_WATCH_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

    def seed(self, **session):
        """Write a v1 state holding SID with the given session fields."""
        state = {"schemaVersion": 1, "servers": {}, "sessions": {SID: session}}
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)

    def state_bytes(self):
        if not os.path.exists(self.state_file):
            return None
        with open(self.state_file, "rb") as fh:
            return fh.read()

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

    def ledger(self):
        return self.dump_state().get("sessions", {}).get(SID, {}).get("ledger", {})

    # --- assertions ------------------------------------------------------------------------

    def assert_silent(self, proc):
        """The hook has no output channel at all: nothing on stdout, nothing on stderr."""
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def assert_just_now(self, stamp):
        parsed = ledger_mod.parse_iso(stamp)
        self.assertIsNotNone(parsed, msg="not a timestamp: %r" % (stamp,))
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
        self.assertLess(abs(age), 120, msg="%s is not recent (%.0fs old)" % (stamp, age))

    def assert_untouched(self, payload):
        """The hook saw `payload`, said nothing and left the state file exactly as it was --
        including not creating one."""
        before = self.state_bytes()
        self.assert_silent(self.run_hook(payload))
        self.assertEqual(self.state_bytes(), before)


class ScribeDispatch(BriefWatchTestCase):
    def test_a_scribe_dispatch_sets_the_brief_and_remembers_the_agent(self):
        self.assert_silent(self.run_hook(dispatch()))
        led = self.ledger()
        self.assert_just_now(led["briefAt"])
        self.assertEqual(led["scribeIds"], [AGENT_ID])

    def test_the_older_task_tool_name_counts(self):
        self.assert_silent(self.run_hook(dispatch(tool_name="Task")))
        self.assert_just_now(self.ledger()["briefAt"])

    def test_a_plugin_namespaced_scribe_counts(self):
        self.assert_silent(self.run_hook(dispatch(subagent_type="gitian-kb:kb-scribe")))
        self.assert_just_now(self.ledger()["briefAt"])

    def test_the_agent_id_is_found_in_every_plausible_response_shape(self):
        shapes = {
            "dict key": {"status": "async_launched", "agentId": AGENT_ID, "prompt": "..."},
            "text line": "Async agent launched successfully.\nagentId: %s (internal ID - do not "
            "mention to user. Use SendMessage with to: '%s' to continue this agent.)"
            % (AGENT_ID, AGENT_ID),
            "bare list of text blocks": [
                {"type": "text", "text": "Launched.\nagentId: %s (use SendMessage)" % AGENT_ID}
            ],
            "dict with content blocks": {
                "status": "completed",
                "content": [{"type": "text", "text": "agentId: %s\ndone" % AGENT_ID}],
            },
            "json text": '{"status": "async_launched", "agentId": "%s"}' % AGENT_ID,
            "json text with an escaped newline": json.dumps(
                [{"type": "text", "text": "Launched.\nagentId: %s (use SendMessage)" % AGENT_ID}]
            ),
            "json inside json": json.dumps({"result": json.dumps({"agentId": AGENT_ID})}),
            "equals sign": "agentId=%s" % AGENT_ID,
        }
        for label, response in shapes.items():
            with self.subTest(shape=label):
                self.seed()
                self.assert_silent(self.run_hook(dispatch(tool_response=response)))
                self.assertEqual(self.ledger()["scribeIds"], [AGENT_ID])

    def test_a_dispatch_without_an_id_still_records_the_brief(self):
        for response in ({"status": "completed"}, "done", [], 42, None):
            with self.subTest(response=response):
                self.seed()
                payload = dispatch(tool_response=response)
                if response is None:
                    del payload["tool_response"]
                self.assert_silent(self.run_hook(payload))
                led = self.ledger()
                self.assert_just_now(led["briefAt"])
                self.assertEqual(led["scribeIds"], [])

    def test_only_six_to_sixty_four_id_characters_make_an_id(self):
        for label, candidate in (("too short", "a1b2c"), ("too long", "a" * 65)):
            with self.subTest(case=label):
                self.seed()
                self.assert_silent(self.run_hook(dispatch(tool_response={"agentId": candidate})))
                self.assertEqual(self.ledger()["scribeIds"], [])
                self.seed()
                text = "agentId: %s (internal)" % candidate
                self.assert_silent(self.run_hook(dispatch(tool_response=text)))
                self.assertEqual(self.ledger()["scribeIds"], [])
        for label, candidate in (
            ("shortest", "a1b2c3"),
            ("longest", "b" * 64),
            ("dashes", "a-b_c-d_e"),
        ):
            with self.subTest(case=label):
                self.seed()
                self.assert_silent(self.run_hook(dispatch(tool_response={"agentId": candidate})))
                self.assertEqual(self.ledger()["scribeIds"], [candidate])

    def test_the_responses_own_agent_id_beats_one_quoted_in_its_text(self):
        # The scribe's report can mention another agent; the dispatch's own id is the key.
        response = {
            "content": [{"type": "text", "text": "I also asked agentId: %s to look" % OTHER_ID}],
            "agentId": AGENT_ID,
        }
        self.assert_silent(self.run_hook(dispatch(tool_response=response)))
        self.assertEqual(self.ledger()["scribeIds"], [AGENT_ID])

    def test_a_word_ending_in_agent_id_is_not_the_key(self):
        self.assert_silent(self.run_hook(dispatch(tool_response="subagentId: %s" % AGENT_ID)))
        self.assertEqual(self.ledger()["scribeIds"], [])

    def test_a_later_dispatch_moves_the_brief_and_keeps_the_earlier_ids(self):
        self.seed(ledger={"briefAt": ago(hours=2), "scribeIds": [OTHER_ID]})
        self.assert_silent(self.run_hook(dispatch()))
        led = self.ledger()
        self.assert_just_now(led["briefAt"])
        self.assertEqual(led["scribeIds"], [OTHER_ID, AGENT_ID])

    def test_the_remembered_ids_are_capped_at_the_newest_five(self):
        for index in range(7):
            payload = dispatch(tool_response={"agentId": "scribe-%02d-xyz" % index})
            self.assert_silent(self.run_hook(payload))
        self.assertEqual(
            self.ledger()["scribeIds"], ["scribe-%02d-xyz" % index for index in range(2, 7)]
        )

    def test_a_brief_covers_the_activity_before_it(self):
        # End to end through ledger.py: a commit made 5 minutes ago owes a KB write until the
        # primary hands it to the scribe, and not after.
        self.seed(
            ledger={
                "events": [
                    {"at": ago(minutes=5), "kind": "commit", "repo": "acme/api", "ref": "abc1234"}
                ]
            }
        )
        now = ledger_mod.utcnow()
        before = self.dump_state()["sessions"][SID]
        self.assertTrue(ledger_mod.has_open_qualifying(before, now))

        self.assert_silent(self.run_hook(dispatch()))
        after = self.dump_state()["sessions"][SID]
        self.assertFalse(ledger_mod.has_open_qualifying(after, ledger_mod.utcnow()))
        self.assertEqual(len(after["ledger"]["events"]), 1)  # pending, not yet pruned


class NonScribeDispatch(BriefWatchTestCase):
    def test_other_agents_are_not_briefs(self):
        for subagent_type in (
            "kb-librarian",
            "gitian-kb:kb-librarian",
            "general-purpose",
            "Explore",
            "kb-scribe-2",
            "my-kb-scribe",
            "kb-scribe:extra",
            "KB-SCRIBE",
            "",
            None,
            5,
            ["kb-scribe"],
        ):
            with self.subTest(subagent_type=subagent_type):
                self.assert_untouched(dispatch(subagent_type=subagent_type))
        self.assertIsNone(self.state_bytes())  # no state file was ever created

    def test_a_dispatch_with_no_subagent_type_is_not_a_brief(self):
        payload = dispatch()
        del payload["tool_input"]["subagent_type"]
        self.assert_untouched(payload)
        payload["tool_input"] = "kb-scribe"
        self.assert_untouched(payload)


class FollowUpMessage(BriefWatchTestCase):
    def test_a_message_to_a_known_scribe_is_a_brief(self):
        self.seed(ledger={"briefAt": ago(minutes=40), "scribeIds": [AGENT_ID]})
        self.assert_silent(self.run_hook(message(AGENT_ID)))
        self.assert_just_now(self.ledger()["briefAt"])

    def test_the_id_a_dispatch_recorded_is_what_a_message_matches(self):
        self.assert_silent(self.run_hook(dispatch()))
        led = self.ledger()
        led["briefAt"] = ago(minutes=40)  # time passes
        self.seed(ledger=led)
        self.assert_silent(self.run_hook(message(AGENT_ID)))
        self.assert_just_now(self.ledger()["briefAt"])

    def test_a_message_to_anyone_else_is_nothing(self):
        self.seed(ledger={"briefAt": ago(minutes=40), "scribeIds": [AGENT_ID]})
        for target in (OTHER_ID, "researcher", "*", "", "kb-scribe"):
            with self.subTest(to=target):
                self.assert_untouched(message(target))

    def test_a_message_with_no_usable_target_is_nothing(self):
        self.seed(ledger={"briefAt": ago(minutes=40), "scribeIds": [AGENT_ID]})
        for target in (None, 7, [AGENT_ID], {"id": AGENT_ID}):
            with self.subTest(to=target):
                self.assert_untouched(message(target))
        payload = message(AGENT_ID)
        del payload["tool_input"]["to"]
        self.assert_untouched(payload)

    def test_a_message_in_a_session_that_never_briefed_anyone_is_nothing(self):
        self.assert_untouched(message(AGENT_ID))
        self.assertIsNone(self.state_bytes())


class Guards(BriefWatchTestCase):
    def test_a_subagents_dispatch_or_message_is_ignored(self):
        self.seed(ledger={"briefAt": ago(hours=1), "scribeIds": [AGENT_ID]})
        self.assert_untouched(dispatch(agent_id="agent-123", agent_type="general-purpose"))
        self.assert_untouched(message(AGENT_ID, agent_id="agent-123"))

    def test_every_other_tool_leaves_the_state_alone(self):
        for tool_name in ("Bash", "Edit", "Write", "Read", "mcp__plugin_gitian-kb_gitian-kb__get"):
            with self.subTest(tool=tool_name):
                # A look-alike input must not matter: the tool name decides.
                payload = dispatch(tool_name=tool_name)
                payload["tool_input"]["to"] = AGENT_ID
                self.assert_untouched(payload)
        self.assertIsNone(self.state_bytes())

    def test_a_payload_without_a_session_id_is_ignored(self):
        payload = dispatch()
        del payload["session_id"]
        self.assert_untouched(payload)
        payload["session_id"] = ""
        self.assert_untouched(payload)
        payload["session_id"] = 12
        self.assert_untouched(payload)
        self.assertIsNone(self.state_bytes())

    def test_it_never_prints_anything(self):
        self.seed(ledger={"scribeIds": [AGENT_ID]})
        for payload in (
            dispatch(),
            dispatch(tool_name="Task"),
            message(AGENT_ID),
            message("nobody"),
        ):
            self.assert_silent(self.run_hook(payload))


class Robustness(BriefWatchTestCase):
    def test_empty_stdin_is_silent(self):
        self.assert_untouched("")

    def test_garbage_and_non_object_stdin_is_silent(self):
        for raw in ("{not valid json ][ at all", "[1, 2, 3]", '"a string"', "null", "42"):
            with self.subTest(stdin=raw):
                self.assert_untouched(raw)

    def test_a_non_dict_tool_input_is_silent(self):
        for tool_input in ("kb-scribe", ["kb-scribe"], None, 3):
            with self.subTest(tool_input=tool_input):
                payload = dispatch()
                payload["tool_input"] = tool_input
                self.assert_untouched(payload)

    def test_a_deeply_nested_response_is_survived(self):
        response = {"agentId": 7}
        for _ in range(50):
            response = {"next": [response]}
        self.assert_silent(self.run_hook(dispatch(tool_response=response)))
        led = self.ledger()
        self.assert_just_now(led["briefAt"])
        self.assertEqual(led["scribeIds"], [])

    def test_a_corrupt_state_file_is_rebuilt_silently(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")
        self.assert_silent(self.run_hook(dispatch()))
        self.assert_just_now(self.ledger()["briefAt"])

    def test_an_unwritable_state_path_is_silent(self):
        # The state file's path resolves to a directory, so the locked write cannot land. The
        # failure must not surface: no traceback, no output, exit 0.
        os.makedirs(self.state_file, exist_ok=True)
        self.assert_silent(self.run_hook(dispatch()))
        self.assert_silent(self.run_hook(message(AGENT_ID)))

    def test_a_malformed_session_record_is_survived(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump({"schemaVersion": 1, "servers": {}, "sessions": {SID: "garbage"}}, fh)
        self.assert_silent(self.run_hook(dispatch()))
        self.assert_just_now(self.ledger()["briefAt"])

        self.seed(ledger={"scribeIds": "not-a-list", "briefAt": 5})
        self.assert_silent(self.run_hook(message(AGENT_ID)))
        self.assert_silent(self.run_hook(dispatch()))
        led = self.ledger()
        self.assert_just_now(led["briefAt"])
        self.assertEqual(led["scribeIds"], [AGENT_ID])


class PreToolUseBrief(BriefWatchTestCase):
    """The brief is recorded when the dispatch STARTS: a foreground scribe publishes before its
    dispatch returns, and those publishes need a brief to confirm. The PRs it names are noted."""

    def test_a_dispatch_starting_records_the_brief_and_the_prs_it_names(self):
        payload = dispatch(hook_event_name="PreToolUse")
        payload.pop("tool_response")
        payload["tool_input"]["prompt"] = "PR #650 merged (see https://github.com/acme/w/pull/651)."
        self.assert_silent(self.run_hook(payload))
        led = self.ledger()
        self.assertIn("briefAt", led)
        self.assertEqual(sorted(b["n"] for b in led["briefedPRs"]), [650, 651])
        self.assertEqual(led["scribeIds"], [])  # no response yet, so no id

    def test_a_message_to_a_live_scribe_notes_the_prs_it_names(self):
        self.seed(ledger={"scribeIds": [AGENT_ID], "events": [], "edits": []})
        payload = message(AGENT_ID, hook_event_name="PreToolUse")
        payload["tool_input"]["message"] = "Also #77 merged."
        self.assert_silent(self.run_hook(payload))
        self.assertEqual([b["n"] for b in self.ledger()["briefedPRs"]], [77])


if __name__ == "__main__":
    unittest.main()
