#!/usr/bin/env python3
"""Release-gate invariant tests for the gitian-kb nudge layer.

Drives the REAL hook scripts end to end via `sh <hook>.sh` (never their python modules directly),
matching the idiom every other test_*.py file in this directory already uses: GITIAN_KB_STATE_FILE
(and HOME, defensively -- see below) point at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other or with a developer's real
session state.

Three scenarios (see the task's pinned spec -- this file IS the release gate):

  A. CompliantSessionEmitsZeroNudges
     A fully compliant session's walk through session-start, two gitian reads, an edit, a
     publish with real cached topics, an append, a commit, and Stop must never emit a single
     nudge. SessionStart's additionalContext is allowed (it is context, not a nudge) -- the
     assertion there is "valid JSON, not shaped like a PreToolUse deny or a Stop block", not
     "empty stdout".

  B. EveryNudgeFiresOnceThenRearmsOnEpochBump
     Every nudge-capable hook fires exactly once per (session, epoch): an identical re-send (or
     a repeat of the same signal) passes silently, and only an epoch bump (simulating `clear`)
     re-arms them.

  C. FailOpenSweep
     Every hook script under plugins/gitian-kb/hooks/*.sh -- swept by glob, not a hardcoded
     list, so a hook added by a later task is automatically covered -- must exit 0 with
     empty-or-valid-JSON stdout under: empty stdin, garbage stdin, valid stdin against a
     corrupt state file, and valid stdin against an unwritable state directory. A nudge hook
     must never crash a session.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_invariant.py
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
STATE_PY = HOOKS_DIR / "state.py"

SESSION_CONTEXT_SH = HOOKS_DIR / "session-context.sh"
HARVEST_SH = HOOKS_DIR / "harvest.sh"
ORIENTATION_SH = HOOKS_DIR / "orientation-check.sh"
PUBLISH_LINT_SH = HOOKS_DIR / "publish-lint.sh"
COMMIT_NUDGE_SH = HOOKS_DIR / "commit-nudge.sh"
PUBLISH_REMINDER_SH = HOOKS_DIR / "publish-reminder.sh"

# Every hook script that ships in this directory -- Scenario C sweeps this list (not a hardcoded
# one) so a hook added by a later task is automatically covered by the fail-open gate.
ALL_HOOK_SCRIPTS = sorted(HOOKS_DIR.glob("*.sh"))

SERVER_KEY = "https://gitian.dev/api/mcp"  # default GITIAN_KB_URL, per the state contract


# --- envelope builders -- one per hook's stdin contract, mirroring each hook's own test file -----


def session_start_envelope(source="startup", session_id="sess-1", cwd="/repo"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": cwd,
        "hook_event_name": "SessionStart",
        "source": source,
    }


def harvest_envelope(tool_name, tool_input=None, tool_response=None, session_id="sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {},
        "tool_response": tool_response if tool_response is not None else {},
    }


def orientation_envelope(tool_input=None, session_id="sess-1", cwd="/repo"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_input": tool_input if tool_input is not None else {"file_path": "/x"},
    }


def lint_envelope(tool_name, tool_input=None, session_id="sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {},
    }


def commit_envelope(command, tool_response=None, session_id="sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": tool_response if tool_response is not None else {"stdout": "ok", "stderr": ""},
    }


def stop_envelope(transcript_path, session_id="sess-1", stop_hook_active=False):
    return {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": "/repo",
        "hook_event_name": "Stop",
        "stop_hook_active": stop_hook_active,
    }


def minted_response(slugs, slug="new-doc"):
    """Real MCP wire shape for the organic_topics_minted warning (see test_mint_followup.py):
    a LintWarning nested inside an MCP content text block."""
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


def _tool_use(name, tool_input=None):
    return {"type": "tool_use", "name": name, "input": tool_input if tool_input is not None else {}}


def _assistant_line(blocks):
    return {"type": "assistant", "message": {"role": "assistant", "content": blocks}}


def edit_line(name="Edit"):
    return _assistant_line([_tool_use(name, {"file_path": "/x", "old_string": "a", "new_string": "b"})])


def publish_line(tool_name="mcp__plugin_gitian-kb_gitian__publish_doc"):
    return _assistant_line([_tool_use(tool_name, {"slug": "x"})])


class InvariantTestCase(unittest.TestCase):
    """Shared plumbing for all three scenarios: fresh tempdir state per test, subprocess helpers
    that drive the real `sh <hook>.sh` scripts, and the generic silence/deny/fire/block
    assertions every hook's own test file already uses."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-invariant-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        # Defensive, per the task spec: even though every hook resolves its state path via
        # GITIAN_KB_STATE_FILE (always set above), point HOME at the tempdir too so nothing in
        # this suite could ever fall back to touching a developer's real ~/.claude/gitian-kb.
        self.env["HOME"] = self.tmpdir
        self.env.pop("GITIAN_KB_URL", None)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # --- invocation -------------------------------------------------------------------------

    def run_hook(self, script, payload, env=None):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(script)],
            input=input_text,
            capture_output=True,
            text=True,
            env=env if env is not None else self.env,
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

    def merge(self, doc):
        proc = self.run_state("merge", input_text=json.dumps(doc))
        self.assertEqual(proc.returncode, 0)
        return proc

    def seed_vocab(self, topics):
        self.merge({"servers": {SERVER_KEY: {"topics": topics}}})

    def bump_epoch(self, sid):
        proc = self.run_state("bump-epoch", sid)
        self.assertEqual(proc.returncode, 0)
        return proc

    def dump_state(self):
        proc = self.run_state("dump")
        self.assertEqual(proc.returncode, 0)
        return json.loads(proc.stdout) if proc.stdout.strip() else {}

    def write_transcript(self, line_objs):
        path = os.path.join(self.tmpdir, "transcript-%d.jsonl" % len(os.listdir(self.tmpdir)))
        with open(path, "w", encoding="utf-8") as fh:
            for obj in line_objs:
                fh.write(json.dumps(obj))
                fh.write("\n")
        return path

    # --- assertions -------------------------------------------------------------------------

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stdout=%r stderr=%r" % (proc.stdout, proc.stderr))
        self.assertEqual(proc.stdout, "")

    def assert_context_not_a_nudge(self, proc):
        """SessionStart's additionalContext is allowed content, not a nudge -- valid JSON whose
        shape is neither a PreToolUse deny (`hookSpecificOutput.permissionDecision`) nor a Stop
        block (`decision`/`reason`). Checked structurally (parsed keys), not by substring search,
        so prose that happens to contain the word "decision" can never trip a false positive."""
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertNotIn("decision", payload)
        hso = payload.get("hookSpecificOutput")
        if isinstance(hso, dict):
            self.assertNotIn("permissionDecision", hso)
        return payload

    def assert_deny(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        return out["permissionDecisionReason"]

    def assert_post_tool_context(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        return out["additionalContext"]

    def assert_block(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload.get("decision"), "block")
        self.assertTrue(payload.get("reason"))
        return payload["reason"]

    def assert_fail_open(self, proc):
        """The Scenario C contract: exit 0, and stdout is either empty or one well-formed JSON
        object -- never a traceback, never a non-zero exit, never partial/malformed output."""
        self.assertEqual(proc.returncode, 0, msg="stdout=%r stderr=%r" % (proc.stdout, proc.stderr))
        if proc.stdout.strip():
            json.loads(proc.stdout)


class CompliantSessionEmitsZeroNudges(InvariantTestCase):
    """Scenario A -- the release gate itself: a session that does everything right (reads before
    editing, links real cached topics, journals near its commit) must never see a single nudge."""

    def test_fully_compliant_session_produces_no_nudge_output_anywhere(self):
        sid = "sess-compliant"

        # 1. SessionStart(startup) -- context is allowed content, not a nudge.
        start = self.run_hook(SESSION_CONTEXT_SH, session_start_envelope(session_id=sid, cwd=self.tmpdir))
        self.assert_context_not_a_nudge(start)

        # 2. Harvest a search read -- seeds gitianReads.
        search = self.run_hook(
            HARVEST_SH,
            harvest_envelope(
                "mcp__plugin_gitian-kb_gitian__search",
                tool_input={"query": "auth"},
                tool_response={"ok": True, "results": []},
                session_id=sid,
            ),
        )
        self.assert_silent(search)

        # 3. Harvest a gitian-kb://vocab resource read -- seeds the vocab cache + another read.
        vocab_topics = [
            {"slug": "auth", "description": "Authentication flows", "degree": 5},
            {"slug": "billing", "description": "Billing & invoices", "degree": 3},
        ]
        vocab = self.run_hook(
            HARVEST_SH,
            harvest_envelope(
                "ReadMcpResourceTool",
                tool_input={"uri": "gitian-kb://vocab"},
                tool_response={
                    "contents": [
                        {"uri": "gitian-kb://vocab", "text": json.dumps({"topics": vocab_topics})}
                    ]
                },
                session_id=sid,
            ),
        )
        self.assert_silent(vocab)

        state = self.dump_state()
        self.assertEqual(state["sessions"][sid]["gitianReads"], 2)

        # 4. Orientation-check on an Edit -- silent, reads > 0.
        orientation = self.run_hook(ORIENTATION_SH, orientation_envelope(session_id=sid, cwd=self.tmpdir))
        self.assert_silent(orientation)

        # 5. Publish lint on a publish_doc with 2 valid cached topics -- silent.
        lint = self.run_hook(
            PUBLISH_LINT_SH,
            lint_envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"title": "Auth billing notes", "topics": ["auth", "billing"]},
                session_id=sid,
            ),
        )
        self.assert_silent(lint)

        # 6. Harvest the publish success envelope -- no organic_topics_minted, so no mint prompt.
        publish = self.run_hook(
            HARVEST_SH,
            harvest_envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"title": "Auth billing notes", "topics": ["auth", "billing"]},
                tool_response={"isError": False, "slug": "auth-billing-notes"},
                session_id=sid,
            ),
        )
        self.assert_silent(publish)

        # 7. Harvest an append_entry success -- sets lastAppendAt to now, arms the commit damper.
        append = self.run_hook(
            HARVEST_SH,
            harvest_envelope(
                "mcp__plugin_gitian-kb_gitian__append_entry",
                tool_input={"slug": "journal-2026-07-18"},
                tool_response={"isError": False, "slug": "journal-2026-07-18"},
                session_id=sid,
            ),
        )
        self.assert_silent(append)

        # 8. Commit-nudge on a successful git commit -- silent, append landed within the last 2h.
        commit = self.run_hook(COMMIT_NUDGE_SH, commit_envelope("git commit -m 'wip'", session_id=sid))
        self.assert_silent(commit)

        # 9. Publish-reminder Stop -- silent, this turn's transcript published to the KB.
        transcript = self.write_transcript([edit_line(), edit_line(), edit_line(), publish_line()])
        stop = self.run_hook(PUBLISH_REMINDER_SH, stop_envelope(transcript, session_id=sid))
        self.assert_silent(stop)

        # Every nudge-capable invocation above (4, 5, 8, 9) produced empty stdout, exit 0 -- the
        # release-gate invariant itself, restated as one final blanket assertion.
        for proc in (orientation, lint, commit, stop):
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")

    def test_fresh_install_compliant_session_produces_no_nudge_output_anywhere(self):
        """Variant of the above with an EMPTY/ABSENT state cache from the very start -- no prior
        `gitian-kb://vocab` resource read has EVER seeded `servers.<key>.topics` (a first-ever
        session against a brand-new plugin install, before `kb-librarian` or anything else has
        ever populated the local vocab cache). Publishing with topics the empty cache doesn't
        know about must still be completely silent: r1 (empty-topics) doesn't fire because
        topics ARE supplied, r2 (near-miss) is explicitly gated off when the cache is empty (see
        publish_lint.py's `_check_near_miss`), and r3 (project-name) doesn't fire for a genuine
        concept slug."""
        sid = "sess-fresh-install"

        # 1. SessionStart(startup) -- context is allowed content, not a nudge.
        start = self.run_hook(
            SESSION_CONTEXT_SH, session_start_envelope(session_id=sid, cwd=self.tmpdir)
        )
        self.assert_context_not_a_nudge(start)
        self.assertFalse(os.path.exists(self.state_file))  # nothing has touched state yet

        # 2. Harvest a search read -- seeds gitianReads, WITHOUT ever seeding the vocab cache (no
        # ReadMcpResourceTool call for gitian-kb://vocab anywhere in this session).
        search = self.run_hook(
            HARVEST_SH,
            harvest_envelope(
                "mcp__plugin_gitian-kb_gitian__search",
                tool_input={"query": "greenfield"},
                tool_response={"ok": True, "results": []},
                session_id=sid,
            ),
        )
        self.assert_silent(search)

        state = self.dump_state()
        self.assertEqual(state["sessions"][sid]["gitianReads"], 1)
        self.assertEqual(state.get("servers", {}), {})  # vocab cache never populated

        # 3. Orientation-check on an Edit -- silent, one read is enough.
        orientation = self.run_hook(ORIENTATION_SH, orientation_envelope(session_id=sid, cwd=self.tmpdir))
        self.assert_silent(orientation)

        # 4. Publish lint on a publish_doc naming a topic the (empty) cache has never heard of --
        # silent, per the rule breakdown in this test's own docstring.
        lint = self.run_hook(
            PUBLISH_LINT_SH,
            lint_envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"title": "Greenfield init", "topics": ["greenfield-init"]},
                session_id=sid,
            ),
        )
        self.assert_silent(lint)

        # 5. Harvest the publish success envelope -- no organic_topics_minted, so no mint prompt.
        publish = self.run_hook(
            HARVEST_SH,
            harvest_envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc",
                tool_input={"title": "Greenfield init", "topics": ["greenfield-init"]},
                tool_response={"isError": False, "slug": "greenfield-init-doc"},
                session_id=sid,
            ),
        )
        self.assert_silent(publish)

        # 6. Harvest an append_entry success -- sets lastAppendAt to now, arms the commit damper.
        append = self.run_hook(
            HARVEST_SH,
            harvest_envelope(
                "mcp__plugin_gitian-kb_gitian__append_entry",
                tool_input={"slug": "journal-2026-07-18"},
                tool_response={"isError": False, "slug": "journal-2026-07-18"},
                session_id=sid,
            ),
        )
        self.assert_silent(append)

        # 7. Commit-nudge on a successful git commit -- silent, append landed within the last 2h.
        commit = self.run_hook(COMMIT_NUDGE_SH, commit_envelope("git commit -m 'wip'", session_id=sid))
        self.assert_silent(commit)

        # 8. Publish-reminder Stop -- silent, this turn's transcript published to the KB.
        transcript = self.write_transcript([edit_line(), edit_line(), edit_line(), publish_line()])
        stop = self.run_hook(PUBLISH_REMINDER_SH, stop_envelope(transcript, session_id=sid))
        self.assert_silent(stop)

        for proc in (orientation, lint, commit, stop):
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")


class EveryNudgeFiresOnceThenRearmsOnEpochBump(InvariantTestCase):
    """Scenario B -- every nudge-capable hook fires exactly once per (session, epoch); an
    identical re-send / a repeat of the same signal passes silently; only an epoch bump
    (simulating `clear`) re-arms them."""

    def test_each_nudge_fires_once_then_only_an_epoch_bump_rearms_it(self):
        sid = "sess-rearm"

        # -- orientation: zero reads -> deny once, identical re-send passes ---------------------
        edit_payload = orientation_envelope(session_id=sid, cwd=self.tmpdir)
        first_orientation = self.run_hook(ORIENTATION_SH, edit_payload)
        reason = self.assert_deny(first_orientation)
        self.assertIn("file_intents", reason)

        second_orientation = self.run_hook(ORIENTATION_SH, edit_payload)
        self.assert_silent(second_orientation)

        # -- publish lint r1 (empty topics): deny once with cached candidates, resend passes -----
        self.seed_vocab(
            [
                {"slug": "auth", "description": "Authentication flows", "degree": 5},
                {"slug": "kb-discipline", "description": "KB discipline", "degree": 4},
            ]
        )
        empty_topics_payload = lint_envelope(
            "mcp__plugin_gitian-kb_gitian__publish_doc",
            tool_input={"title": "x", "topics": []},
            session_id=sid,
        )
        first_lint = self.run_hook(PUBLISH_LINT_SH, empty_topics_payload)
        lint_reason = self.assert_deny(first_lint)
        self.assertIn("auth - Authentication flows", lint_reason)

        second_lint = self.run_hook(PUBLISH_LINT_SH, empty_topics_payload)
        self.assert_silent(second_lint)

        # -- publish lint r2 (near-miss): a DIFFERENT call (new hash), still fires once ----------
        near_miss_payload = lint_envelope(
            "mcp__plugin_gitian-kb_gitian__publish_doc",
            tool_input={"title": "y", "topics": ["kb-disciplne"]},
            session_id=sid,
        )
        near_miss = self.run_hook(PUBLISH_LINT_SH, near_miss_payload)
        near_miss_reason = self.assert_deny(near_miss)
        self.assertIn('did you mean "kb-discipline"?', near_miss_reason)

        second_near_miss = self.run_hook(PUBLISH_LINT_SH, near_miss_payload)
        self.assert_silent(second_near_miss)

        # -- commit nudge: no recent append anywhere -> fires once -------------------------------
        commit_payload = commit_envelope("git commit -m 'wip'", session_id=sid)
        first_commit = self.run_hook(COMMIT_NUDGE_SH, commit_payload)
        commit_context = self.assert_post_tool_context(first_commit)
        self.assertIn("append_entry", commit_context)

        second_commit = self.run_hook(COMMIT_NUDGE_SH, commit_payload)
        self.assert_silent(second_commit)

        # -- mint follow-up: fires once per new slug, repeat envelope silent ---------------------
        minted_payload = harvest_envelope(
            "mcp__plugin_gitian-kb_gitian__publish_doc",
            tool_response=minted_response(["auth-flow"]),
            session_id=sid,
        )
        first_mint = self.run_hook(HARVEST_SH, minted_payload)
        mint_context = self.assert_post_tool_context(first_mint)
        self.assertIn("auth-flow", mint_context)

        second_mint = self.run_hook(HARVEST_SH, minted_payload)
        self.assert_silent(second_mint)

        # -- Stop publish reminder: 3 edits, 0 publishes -> block once, repeat silent ------------
        transcript = self.write_transcript([edit_line(), edit_line(), edit_line()])
        stop_payload = stop_envelope(transcript, session_id=sid)
        first_stop = self.run_hook(PUBLISH_REMINDER_SH, stop_payload)
        self.assert_block(first_stop)

        second_stop = self.run_hook(PUBLISH_REMINDER_SH, stop_payload)
        self.assert_silent(second_stop)

        # -- epoch bump (simulating `clear`) re-arms the once-per-epoch nudges -------------------
        self.bump_epoch(sid)

        third_orientation = self.run_hook(ORIENTATION_SH, edit_payload)
        self.assert_deny(third_orientation)


class FailOpenSweep(InvariantTestCase):
    """Scenario C -- every hook script in this directory must fail open: empty stdin, garbage
    stdin, valid stdin against a corrupt state file, and valid stdin against an unwritable state
    directory must all exit 0 with empty-or-valid-JSON stdout. Sweeps `hooks/*.sh` itself rather
    than a hardcoded list, so a hook added by a later task is automatically covered."""

    def _valid_envelope_for(self, script_name, sid, transcript_path):
        """A stdin envelope shaped so the hook's guard passes and its state-touching logic
        actually runs -- the point of this sweep is to stress the read-modify-write path under a
        corrupt/unwritable state substrate, not just exercise the early "not my call" guard."""
        if script_name == "harvest.sh":
            return harvest_envelope(
                "mcp__plugin_gitian-kb_gitian__get",
                tool_input={"slug": "x"},
                tool_response={"ok": True},
                session_id=sid,
            )
        if script_name == "orientation-check.sh":
            return orientation_envelope(session_id=sid)
        if script_name == "publish-lint.sh":
            return lint_envelope(
                "mcp__plugin_gitian-kb_gitian__publish_doc", tool_input={"title": "x"}, session_id=sid
            )
        if script_name == "commit-nudge.sh":
            return commit_envelope("git commit -m 'x'", session_id=sid)
        if script_name == "publish-reminder.sh":
            return stop_envelope(transcript_path, session_id=sid)
        if script_name == "session-context.sh":
            return session_start_envelope(session_id=sid)
        # Defensive fallback for a hook this test doesn't know the shape of yet (added by a later
        # task) -- still a plausible generic envelope, so the sweep at least exercises "does this
        # new hook crash", even without knowing its exact guard conditions.
        return {"session_id": sid, "transcript_path": transcript_path, "cwd": "/repo"}

    def test_every_hook_fails_open_on_every_input_failure_mode(self):
        self.assertTrue(ALL_HOOK_SCRIPTS, "expected at least one hooks/*.sh script to sweep")

        valid_transcript = self.write_transcript([edit_line(), edit_line(), edit_line()])

        for script in ALL_HOOK_SCRIPTS:
            name = script.name

            with self.subTest(script=name, case="empty_stdin"):
                proc = self.run_hook(script, "")
                self.assert_fail_open(proc)

            with self.subTest(script=name, case="garbage_stdin"):
                proc = self.run_hook(script, "not json")
                self.assert_fail_open(proc)

            with self.subTest(script=name, case="corrupt_state_file"):
                tmp = tempfile.mkdtemp(prefix="gks-invariant-corrupt-")
                try:
                    state_file = os.path.join(tmp, "state.json")
                    with open(state_file, "w", encoding="utf-8") as fh:
                        fh.write("{{{")
                    env = dict(self.env)
                    env["GITIAN_KB_STATE_FILE"] = state_file
                    env["HOME"] = tmp
                    payload = self._valid_envelope_for(name, "sess-fo-corrupt", valid_transcript)
                    proc = self.run_hook(script, payload, env=env)
                    self.assert_fail_open(proc)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

            with self.subTest(script=name, case="unwritable_state_dir"):
                tmp = tempfile.mkdtemp(prefix="gks-invariant-unwritable-")
                try:
                    # A regular file occupies the path component the state dir needs to become --
                    # os.makedirs() inside state.py can never create it (same technique as
                    # test_orientation.py's own CorruptStateFailOpen fixture).
                    blocker = os.path.join(tmp, "blocker")
                    with open(blocker, "w", encoding="utf-8") as fh:
                        fh.write("not a directory")
                    env = dict(self.env)
                    env["GITIAN_KB_STATE_FILE"] = os.path.join(blocker, "nested", "state.json")
                    env["HOME"] = tmp
                    payload = self._valid_envelope_for(name, "sess-fo-unwritable", valid_transcript)
                    proc = self.run_hook(script, payload, env=env)
                    self.assert_fail_open(proc)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
