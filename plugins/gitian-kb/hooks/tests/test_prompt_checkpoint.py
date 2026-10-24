#!/usr/bin/env python3
"""Unit tests for prompt_checkpoint.py, the UserPromptSubmit hook that delivers the gitian-kb nudge
layer's cheap checkpoint: a standing-rule hint and a short list of unwritten activity as context on
the user's next message, with no extra turn.

Drives it end to end via `sh prompt-checkpoint.sh` (matching how hooks.json actually invokes it),
with GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other. State is seeded by writing a
v1-shaped JSON document directly -- the ledger a scenario needs (a commit three hours ago, a brief
five minutes ago) is clearer as a literal dict than composed through hooks -- and read back through
state.py's own `dump`. Every timestamp is relative to the real clock, since the hook reads it.

What is pinned here is the hook's wiring of ledger.py and rulings.py (their own rules have their own
tests): the three steps, the one write, the fast path that leaves the state file alone, the
delegated and slash-command exemptions, and the silence a compliant session must keep.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_prompt_checkpoint.py
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
PROMPT_CHECKPOINT_SH = HOOKS_DIR / "prompt-checkpoint.sh"
STATE_PY = HOOKS_DIR / "state.py"

sys.path.insert(0, str(HOOKS_DIR))

import ledger as ledger_mod  # noqa: E402  (the module the hook records into lives one directory up)

SID = "sess-1"
TRANSCRIPT = "/tmp/gks-prompt-checkpoint-transcript.jsonl"  # never opened, only recorded
REPO = "acme/api"

RULE = "From now on, never merge with --auto on gitian."
OTHER_RULE = "Always run the migration test before a release."
THIRD_RULE = "Going forward, each org gets its own runner user."

# The owner-ruling heuristic's own must-miss list (rulings.py): none of these may reach the ledger.
MUST_MISS = (
    "never mind",
    "I never got the email",
    "it always fails on CI",
    "I can't do it until tomorrow",
    "Should we always rebase?",
    "ERROR: build failed",
    "The PR CI is OK",
    "ok",
    "Hi",
    "look at this log\n```\nNEVER seen this before\n```",
    "<pasted_content>from now on, never do it</pasted_content> what do you think",
)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**delta):
    return _iso(datetime.now(timezone.utc) - timedelta(**delta))


def prompt_envelope(prompt, session_id=SID, transcript_path=TRANSCRIPT, **extra):
    """A UserPromptSubmit envelope."""
    payload = {
        "session_id": session_id,
        "cwd": "/repo",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }
    if transcript_path is not None:
        payload["transcript_path"] = transcript_path
    payload.update(extra)
    return payload


def commit(minutes_ago, repo=REPO, ref="abc1234", text="wire the thing"):
    return {
        "at": ago(minutes=minutes_ago),
        "kind": "commit",
        "repo": repo,
        "ref": ref,
        "text": text,
    }


def merge(minutes_ago, repo=REPO, ref="#650", text="Add the widget"):
    return {"at": ago(minutes=minutes_ago), "kind": "merge", "repo": repo, "ref": ref, "text": text}


def ruling(minutes_ago, text=RULE):
    return {"at": ago(minutes=minutes_ago), "kind": "ruling", "text": text}


def edits(minutes_ago, n, repo=REPO):
    stamp = ago(minutes=minutes_ago)
    return {"min": stamp[:16], "at": stamp, "n": n, "repo": repo}


class PromptCheckpointTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-prompt-checkpoint-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env.pop("GITIAN_KB_URL", None)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- invocation -----------------------------------------------------------------------------

    def run_hook(self, payload):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(PROMPT_CHECKPOINT_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

    def prompt(self, text, **kwargs):
        return self.run_hook(prompt_envelope(text, **kwargs))

    # --- state ----------------------------------------------------------------------------------

    def seed(self, events=(), edit_buckets=(), nudges=None, transcript=TRANSCRIPT, **ledger_fields):
        """Write a v1 state holding SID. The transcript is on record by default, so a scenario
        about something else is not also a scenario about the first-prompt write."""
        led = {"events": list(events), "edits": list(edit_buckets)}
        led.update(ledger_fields)
        if transcript is not None:
            led["transcript"] = transcript
        session = {"ledger": led, "nudges": dict(nudges or {})}
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

    def session(self):
        return self.dump_state().get("sessions", {}).get(SID, {})

    def ledger(self):
        return self.session().get("ledger", {})

    def nudges(self):
        return self.session().get("nudges", {})

    def rulings_recorded(self):
        return [e["text"] for e in self.ledger().get("events", []) if e["kind"] == "ruling"]

    # --- assertions -----------------------------------------------------------------------------

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def assert_silent_and_untouched(self, proc):
        """Said nothing AND left the state file byte for byte as it was (or never made one)."""
        before = self.state_bytes()
        self.assert_silent(proc)
        self.assertEqual(self.state_bytes(), before)

    def assert_context(self, proc):
        """Exactly one JSON object of the UserPromptSubmit shape; returns its additionalContext."""
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertEqual(proc.stderr, "")
        self.assertEqual(proc.stdout.count("\n"), 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(list(payload), ["hookSpecificOutput"])
        hso = payload["hookSpecificOutput"]
        self.assertEqual(sorted(hso), ["additionalContext", "hookEventName"])
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        self.assertTrue(hso["additionalContext"])
        self.assertNotIn("decision", payload)  # advice rides on the message, it never blocks
        return hso["additionalContext"]

    def assert_just_now(self, stamp):
        parsed = ledger_mod.parse_iso(stamp)
        self.assertIsNotNone(parsed, msg="not a timestamp: %r" % (stamp,))
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
        self.assertLess(abs(age), 120, msg="%s is not recent (%.0fs old)" % (stamp, age))


class QuietPrompt(PromptCheckpointTestCase):
    def test_a_prompt_with_nothing_due_is_silent_and_leaves_the_state_untouched(self):
        self.seed()
        self.assert_silent_and_untouched(self.prompt("please look at the failing test"))

    def test_the_first_prompt_records_the_transcript_and_nothing_else(self):
        self.assert_silent(self.prompt("please look at the failing test"))
        session = self.session()
        self.assertEqual(session["ledger"]["transcript"], TRANSCRIPT)
        self.assertEqual(session["ledger"]["events"], [])
        self.assertEqual(session["ledger"]["edits"], [])
        self.assertEqual(session["nudges"], {})

    def test_the_transcript_is_written_once_and_then_the_state_is_left_alone(self):
        self.assert_silent(self.prompt("first"))
        self.assert_silent_and_untouched(self.prompt("second"))
        self.assert_silent_and_untouched(self.prompt("third"))

        self.assert_silent(self.prompt("fourth", transcript_path="/tmp/another.jsonl"))
        self.assertEqual(self.ledger()["transcript"], "/tmp/another.jsonl")

    def test_a_payload_without_a_transcript_path_has_nothing_to_record(self):
        self.assert_silent_and_untouched(self.prompt("hello", transcript_path=None))
        self.assertIsNone(self.state_bytes())  # no state file was created for nothing
        self.assert_silent_and_untouched(self.prompt("hello", transcript_path=""))
        self.assert_silent_and_untouched(self.prompt("hello", transcript_path=5))

    def test_prompts_the_ruling_heuristic_must_miss_are_silent_and_record_nothing(self):
        for text in MUST_MISS:
            with self.subTest(prompt=text):
                self.seed()
                self.assert_silent_and_untouched(self.prompt(text))
                self.assertEqual(self.rulings_recorded(), [])


class RulingHint(PromptCheckpointTestCase):
    def test_a_standing_rule_gets_one_hint_line_and_is_recorded(self):
        context = self.assert_context(self.prompt(RULE))
        self.assertIn("standing rule", context)
        self.assertIn(RULE, context)
        self.assertIn("kb-scribe", context)
        # One line: every hint is context the agent pays for.
        self.assertEqual(context.count("\n"), 0)

        self.assertEqual(self.rulings_recorded(), [RULE])
        self.assert_just_now(self.nudges()["rulingAt"])
        # A hint is not a ledger nudge: it must not spend the checkpoint's `nudges.at`.
        self.assertNotIn("at", self.nudges())
        self.assertEqual(self.ledger()["transcript"], TRANSCRIPT)

    def test_the_heuristics_must_hit_prompts_are_heard(self):
        for text in (
            "DO NOT USE FABLE TO WRITE CODE USE SONNET 5",
            "releases are never posted before prod is fully deployed",
            "Never merge with --auto on gitian.",
            "X cannot happen until Y is green",
            "you must never run remote work as the owner",
            THIRD_RULE,
        ):
            with self.subTest(prompt=text):
                self.seed()
                context = self.assert_context(self.prompt(text))
                self.assertIn("standing rule", context)
                self.assertEqual(self.rulings_recorded(), [text])

    def test_the_hint_fires_once_then_floors_then_returns(self):
        self.assert_context(self.prompt(RULE))

        # A second rule right behind it: recorded for the checkpoints to list, but no second hint.
        self.assert_silent(self.prompt(OTHER_RULE))
        self.assertEqual(self.rulings_recorded(), [RULE, OTHER_RULE])

        # Past the floor (10 minutes) the next rule is hinted again.
        state = self.dump_state()
        state["sessions"][SID]["nudges"]["rulingAt"] = ago(minutes=11)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        context = self.assert_context(self.prompt(THIRD_RULE))
        self.assertIn(THIRD_RULE, context)
        self.assertEqual(self.rulings_recorded(), [RULE, OTHER_RULE, THIRD_RULE])
        self.assert_just_now(self.nudges()["rulingAt"])

    def test_a_restated_rule_is_recorded_once(self):
        self.seed(events=[ruling(3)], nudges={"rulingAt": ago(minutes=30)})
        self.assert_context(self.prompt(RULE))  # past the floor: hinted again
        self.assertEqual(self.rulings_recorded(), [RULE])

    def test_a_rule_alone_never_makes_a_checkpoint(self):
        self.assert_context(self.prompt(RULE))
        self.assertNotIn("at", self.nudges())
        self.assert_silent_and_untouched(self.prompt("thanks, carry on"))

    def test_a_slash_command_carries_no_rule(self):
        self.seed()
        self.assert_silent_and_untouched(self.prompt("/gitian-kb:handoff " + RULE))
        self.assert_silent_and_untouched(self.prompt("  /review " + OTHER_RULE))
        self.assertEqual(self.rulings_recorded(), [])

    def test_an_open_rule_is_listed_by_a_later_checkpoint(self):
        self.seed(events=[ruling(60), commit(150)], nudges={"at": ago(hours=3)})
        text = self.assert_context(self.prompt("carry on"))
        self.assertIn("- the user:", text)
        self.assertIn(RULE, text)


class WorkCheckpoint(PromptCheckpointTestCase):
    def test_work_past_the_cooldown_arrives_as_context_and_marks_the_nudge(self):
        self.seed(events=[commit(150)], nudges={"at": ago(hours=3)})
        text = self.assert_context(self.prompt("what next?"))
        self.assertTrue(text.startswith("gitian-kb checkpoint: activity since "))
        self.assertIn(REPO, text)
        self.assertIn("1 commit", text)
        self.assertIn("abc1234", text)
        self.assertIn("kb-scribe", text)
        self.assertNotIn("standing rule", text)

        self.assert_just_now(self.nudges()["at"])
        self.assertEqual(len(self.ledger()["events"]), 1)  # nudged, not covered: nothing is pruned

    def test_the_same_work_is_not_delivered_twice(self):
        self.seed(events=[commit(150)], nudges={"at": ago(hours=3)})
        self.assert_context(self.prompt("what next?"))
        self.assert_silent_and_untouched(self.prompt("and then?"))

    def test_new_work_after_a_nudge_waits_out_the_cooldown(self):
        self.seed(events=[commit(150), commit(30, ref="def5678")], nudges={"at": ago(minutes=100)})
        self.assert_silent_and_untouched(self.prompt("what next?"))

    def test_work_inside_the_cooldown_waits(self):
        self.seed(events=[commit(30)], nudges={"at": ago(minutes=90)})
        self.assert_silent_and_untouched(self.prompt("what next?"))

    def test_the_sessions_first_work_is_delivered_at_the_next_prompt(self):
        self.seed(events=[commit(10)])
        text = self.assert_context(self.prompt("what next?"))
        self.assertIn("1 commit", text)
        self.assert_just_now(self.nudges()["at"])

    def test_edits_count_once_there_are_three(self):
        self.seed(edit_buckets=[edits(10, 2)])
        self.assert_silent_and_untouched(self.prompt("what next?"))
        self.seed(edit_buckets=[edits(10, 2), edits(5, 1)])
        text = self.assert_context(self.prompt("what next?"))
        self.assertIn("3 file edits", text)

    def test_a_merge_past_the_completion_floor_is_delivered(self):
        self.seed(events=[merge(5)], nudges={"at": ago(minutes=60)})
        text = self.assert_context(self.prompt("what next?"))
        self.assertIn("merged #650", text)

    def test_a_merge_inside_the_completion_floor_waits(self):
        self.seed(events=[merge(5)], nudges={"at": ago(minutes=10)})
        self.assert_silent_and_untouched(self.prompt("what next?"))

    def test_a_draft_release_and_a_new_pull_request_are_information_only(self):
        self.seed(
            events=[
                {"at": ago(minutes=20), "kind": "pr", "repo": REPO, "ref": "#7", "text": "x"},
                {
                    "at": ago(minutes=15),
                    "kind": "release",
                    "repo": REPO,
                    "ref": "v1",
                    "draft": True,
                },
            ]
        )
        self.assert_silent_and_untouched(self.prompt("what next?"))


class CompliantSession(PromptCheckpointTestCase):
    """The release gate: an agent that briefed the scribe after its work sees nothing."""

    def test_a_brief_after_the_activity_keeps_the_prompt_silent(self):
        self.seed(events=[commit(20), merge(18)], briefAt=ago(minutes=5))
        self.assert_silent_and_untouched(self.prompt("what next?"))

    def test_a_content_write_after_the_activity_keeps_the_prompt_silent(self):
        # The commit predates the checkpoint a KB write set, so it is covered whether or not the
        # write also pruned it from the ledger.
        self.seed(events=[commit(200)], nudges={"at": ago(hours=4)}, checkpointAt=ago(minutes=1))
        self.assert_silent_and_untouched(self.prompt("what next?"))

    def test_a_brief_that_never_published_stops_covering_and_the_work_resurfaces(self):
        self.seed(events=[commit(190)], briefAt=ago(minutes=185))
        text = self.assert_context(self.prompt("what next?"))
        self.assertIn("1 commit", text)

    def test_only_the_activity_after_a_checkpoint_is_listed_once_it_is_due(self):
        # A KB write 150 minutes ago covered the old commit; the newer one is past it and the
        # quiet point is more than the 2h cooldown old, so it is delivered -- alone.
        self.seed(
            events=[commit(200, ref="old0000"), commit(10, ref="def5678")],
            checkpointAt=ago(minutes=150),
        )
        text = self.assert_context(self.prompt("what next?"))
        self.assertIn("def5678", text)
        self.assertNotIn("old0000", text)


class RulingAndCheckpointTogether(PromptCheckpointTestCase):
    def test_a_due_checkpoint_carries_the_rule_once_instead_of_a_separate_hint(self):
        # Every line is context the agent pays for: the checkpoint already lists the rule as a
        # `- the user:` line, so the hint is left out rather than saying it twice.
        self.seed(events=[commit(150)], nudges={"at": ago(hours=3)})
        context = self.assert_context(self.prompt(RULE))
        self.assertNotIn("this reads like a standing rule", context)
        self.assertTrue(context.startswith("gitian-kb checkpoint: activity since "))
        self.assertEqual(context.count("- the user:"), 1)
        self.assert_just_now(self.nudges()["rulingAt"])
        self.assert_just_now(self.nudges()["at"])
        self.assertEqual(self.rulings_recorded(), [RULE])

    def test_a_hint_inside_the_floor_leaves_just_the_checkpoint(self):
        self.seed(events=[commit(150)], nudges={"at": ago(hours=3), "rulingAt": ago(minutes=2)})
        context = self.assert_context(self.prompt(RULE))
        self.assertNotIn("this reads like a standing rule", context)
        self.assertTrue(context.startswith("gitian-kb checkpoint: activity since "))
        self.assertIn("- the user:", context)  # the rule still reaches the agent, in the list


class DelegatedSession(PromptCheckpointTestCase):
    """The nudge's remedy is "brief kb-scribe", which a subagent has no tool for."""

    def test_a_subagent_payload_is_silent_and_touches_nothing(self):
        self.seed(events=[commit(150)], nudges={"at": ago(hours=3)})
        for extra in ({"agent_id": "agent-123"}, {"agent_type": "general-purpose"}):
            with self.subTest(extra=extra):
                self.assert_silent_and_untouched(self.prompt("what next?", **extra))
                self.assert_silent_and_untouched(self.prompt(RULE, **extra))
        self.assertEqual(self.rulings_recorded(), [])

        # ... and the primary still gets its nudge afterwards.
        self.assert_context(self.prompt("what next?"))


class Robustness(PromptCheckpointTestCase):
    def test_empty_stdin_is_silent(self):
        self.assert_silent_and_untouched(self.run_hook(""))

    def test_garbage_and_non_object_stdin_is_silent(self):
        for raw in ("{not valid json ][ at all", "[1, 2, 3]", '"a string"', "null", "42"):
            with self.subTest(stdin=raw):
                self.assert_silent_and_untouched(self.run_hook(raw))

    def test_a_missing_or_bad_session_id_is_silent(self):
        for session_id in (None, "", 12, ["x"]):
            with self.subTest(session_id=session_id):
                payload = prompt_envelope(RULE)
                payload["session_id"] = session_id
                self.assert_silent_and_untouched(self.run_hook(payload))
        payload = prompt_envelope(RULE)
        del payload["session_id"]
        self.assert_silent_and_untouched(self.run_hook(payload))
        self.assertIsNone(self.state_bytes())

    def test_a_missing_or_non_string_prompt_is_not_a_ruling(self):
        self.seed()
        for value in (None, 5, ["from now on, never"], {"text": RULE}):
            with self.subTest(prompt=value):
                self.assert_silent_and_untouched(self.run_hook(prompt_envelope(value)))
        payload = prompt_envelope(RULE)
        del payload["prompt"]
        self.assert_silent_and_untouched(self.run_hook(payload))

    def test_a_corrupt_state_file_is_rebuilt_and_the_prompt_still_answered(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")
        context = self.assert_context(self.prompt(RULE))
        self.assertIn("standing rule", context)
        self.assertEqual(self.rulings_recorded(), [RULE])

    def test_an_unwritable_state_path_stays_silent_rather_than_nagging_every_prompt(self):
        # The state file's path resolves to a directory, so the write that would record the nudge
        # cannot land. Printing the hint anyway would repeat it on every prompt: print nothing.
        os.makedirs(self.state_file, exist_ok=True)
        for _ in range(2):
            self.assert_silent(self.prompt(RULE))

    def test_a_malformed_session_record_is_survived(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump({"schemaVersion": 1, "servers": {}, "sessions": {SID: "garbage"}}, fh)
        self.assert_context(self.prompt(RULE))

        for broken in (
            {"ledger": "x", "nudges": 5, "watch": "y"},
            {"ledger": {"events": "x", "edits": 3, "briefAt": "never"}, "nudges": {"at": "never"}},
            {"ledger": {"events": [None, 5, {"kind": "commit"}, {"kind": "commit", "at": "x"}]}},
        ):
            with self.subTest(session=broken):
                with open(self.state_file, "w", encoding="utf-8") as fh:
                    json.dump({"schemaVersion": 1, "servers": {}, "sessions": {SID: broken}}, fh)
                self.assert_silent(self.prompt("what next?"))
                self.assertEqual(self.ledger()["transcript"], TRANSCRIPT)

    def test_a_huge_prompt_is_survived(self):
        self.seed()
        self.assert_silent_and_untouched(self.prompt("word " * 200000))


if __name__ == "__main__":
    unittest.main()
