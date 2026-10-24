#!/usr/bin/env python3
"""Unit tests for session-context.sh + session_digest.py, the gitian-kb nudge layer's SessionStart
source-profile hook.

Drives the hook end to end via `sh session-context.sh` (matching how hooks.json actually invokes
it), with GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other. CLAUDE_PROJECT_DIR points at
that same tempdir too, so the derived repo/branch lines are absent by construction -- these tests
assert on the STATIC context, which must not depend on the checkout the suite happens to run in.
State is seeded by writing a full v1-shaped JSON document directly to that path (same approach as
test_state.py's SevenDayPruning fixture) rather than shelling out to state.py -- these tests care
about exact seed shapes (a specific vocabRev/lastSeenVocabRev pairing, a specific updatedAt age),
which is clearer written as a literal dict than composed through incr/merge calls. The activity
ledger the resume / compact / clear text is built from (ledger.py) is seeded the same way, with
timestamps relative to the real clock because the hook reads it.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_session_context.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
SESSION_CONTEXT_SH = HOOKS_DIR / "session-context.sh"
STATE_PY = HOOKS_DIR / "state.py"

sys.path.insert(0, str(HOOKS_DIR))

import ledger as ledger_mod  # noqa: E402  (the module the hook records into lives one directory up)

SERVER_KEY = "https://gitian.dev/api/mcp/kb"  # default GITIAN_KB_URL, per the state contract

# [[kb-scribe-delegation]]: the static context's budget. The RAG/multi-KB/base_rev/schema-authority
# paragraphs are gone -- the agents carry that discipline now -- and what is left is the delegation
# directive, which has to stay small enough that injecting it into every session is free.
MAX_STATIC_CONTEXT_CHARS = 1200

# Pinned literals every SessionStart source must reproduce verbatim (see
# packages/kb/src/plugin-contract.test.ts and the plan's "honor verbatim" list).
PINNED_RUNTIME_LITERALS = (
    '"hookEventName":"SessionStart"',
    "additionalContext",
    # [[kb-scribe-delegation]]: the two agents by name, and the primary's own half of the split --
    # it decides WHEN and WHAT, never how, and never publishes on its own initiative.
    "kb-librarian",
    "kb-scribe",
    "Never auto-publish",
    # Single-flight: a follow-up reaches the LIVE agent, whose context already holds the draft.
    "SendMessage",
    # The fallback that keeps a subagent (which cannot spawn one) able to publish at all.
    "references/authoring.md",
    # Unchanged from the pre-delegation context, and the one body rule that cannot move into an
    # agent prompt: whoever writes a KB body must never inject gitian markup into a clean repo.
    "NEVER inject gitian markup",
)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_defaults(**overrides):
    session = {
        "epoch": 0,
        "flags": {},
        "gitianReads": 0,
        "edits": 0,
        "publishes": 0,
        "lastSeenVocabRev": {},
        "lintHashes": [],
        "mintPrompted": [],
        "updatedAt": _iso(datetime.now(timezone.utc)),
    }
    session.update(overrides)
    return session


def _topic(slug, description="", degree=1):
    return {"slug": slug, "description": description, "degree": degree}


def _last_seen(rev):
    """Build the sessions.<sid>.lastSeenVocabRev[server][kb] fixture shape (state.py's
    _SESSION_DEFAULTS): a single (SERVER_KEY, "home") observation -- the default bucket every
    reader/writer in this plugin falls back to (see session_digest.py's DEFAULT_KB_SLUG)."""
    return {SERVER_KEY: {"home": rev}}


def _ago(**delta):
    return _iso(datetime.now(timezone.utc) - timedelta(**delta))


def _commit(minutes_ago, repo="acme/api", ref="abc1234", text="wire the thing"):
    return {
        "at": _ago(minutes=minutes_ago),
        "kind": "commit",
        "repo": repo,
        "ref": ref,
        "text": text,
    }


def _watch_entry(repo="acme/web", number=7):
    return {"repo": repo, "number": number, "why": "opened", "addedAt": _ago(hours=1)}


def _ledger_session(events=(), edits=(), nudges=None, watch=(), **ledger_fields):
    """A session whose activity ledger holds the given events / edit buckets (ledger.py shapes)."""
    ledger = {"events": list(events), "edits": list(edits)}
    ledger.update(ledger_fields)
    return _session_defaults(ledger=ledger, nudges=dict(nudges or {}), watch=list(watch))


class SessionContextTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-session-context-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env["CLAUDE_PROJECT_DIR"] = self.tmpdir  # not a git repo: no repo/branch lines
        self.env.pop("GITIAN_KB_URL", None)
        # Hermetic against this machine's real ~/.claude/plugins/known_marketplaces.json: a dev box
        # with auto-update off would otherwise grow every context here by one line.
        self.env["GITIAN_KB_MARKETPLACES_FILE"] = os.path.join(self.tmpdir, "no-such-file.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def seed_state(self, servers=None, sessions=None):
        state = {
            "schemaVersion": 1,
            "servers": servers or {},
            "sessions": sessions or {},
        }
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def write_transcript(self, name="session.jsonl"):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {"content": "hello"}}))
            fh.write("\n")
        return path

    def run_hook(self, payload):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(SESSION_CONTEXT_SH)],
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

    def context_of(self, proc, source_label=""):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:  # pragma: no cover - assertion path
            self.fail("stdout not valid JSON for source=%r: %r (%s)" % (source_label, proc.stdout, exc))
        hso = payload["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        return hso["additionalContext"]

    def envelope(self, source, session_id="sess-1", transcript_path=None):
        payload = {
            "session_id": session_id,
            "cwd": "/repo",
            "hook_event_name": "SessionStart",
            "source": source,
        }
        if transcript_path is not None:
            payload["transcript_path"] = transcript_path
        return payload


class DelegationDirective(SessionContextTestCase):
    def test_every_source_carries_the_delegation_directive_and_nothing_bigger(self):
        for source in ("startup", "resume", "clear", "compact", "some-future-source"):
            proc = self.run_hook(self.envelope(source, session_id="sess-d-%s" % source))
            context = self.context_of(proc, source)
            self.assertIn("kb-librarian", context)
            self.assertIn("kb-scribe", context)
            self.assertIn("Never auto-publish", context)
            self.assertIn("category sweep", context)

    def test_the_retired_pre_delegation_paragraphs_are_gone(self):
        # The discipline they carried lives in the agents now; repeating it here is what made the
        # static context ~2.5 KB on every SessionStart.
        self.seed_state(servers={SERVER_KEY: {"vocabRev": 7, "topics": [_topic("kb", "d", 4)]}})
        context = self.context_of(self.run_hook(self.envelope("startup")), "startup")
        for retired in (
            "RAG discipline",
            "Multi-KB",
            "Schema authority",
            "base_rev_required",
            "own_only_kbs",
            "org_kb_available",
            "KB vocab digest",
        ):
            self.assertNotIn(retired, context, "retired text still injected: %r" % retired)

    def test_static_context_fits_the_budget(self):
        # No repo, no branch, no transcript -> the measured string is the static context plus the
        # one-line UTC date, which is a strictly tighter bound than the budget itself.
        context = self.context_of(self.run_hook(self.envelope("startup")), "startup")
        self.assertLessEqual(
            len(context),
            MAX_STATIC_CONTEXT_CHARS,
            msg="static context is %d chars (budget %d):\n%s"
            % (len(context), MAX_STATIC_CONTEXT_CHARS, context),
        )


class DelegatedSession(SessionContextTestCase):
    """A payload carrying `agent_id` comes from inside a subagent, which can neither dispatch
    kb-librarian nor brief kb-scribe. None of those instructions may reach it -- above all the
    post-compaction "brief kb-scribe for a handoff", which sent delegated sessions after a tool
    they do not have."""

    def delegated(self, source, field="agent_id", value="agent-123"):
        payload = self.envelope(source, session_id="sess-sub")
        payload[field] = value
        return payload

    def test_no_dispatch_or_brief_instruction_on_any_source(self):
        # `agent_id` marks a hook firing inside a subagent; `agent_type` is the field SessionStart
        # documents (a subagent's type, or a `claude --agent <name>` session). Either one gates.
        for field, value in (("agent_id", "agent-123"), ("agent_type", "kb-scribe")):
            for source in ("startup", "resume", "clear", "compact"):
                with self.subTest(field=field, source=source):
                    context = self.context_of(
                        self.run_hook(self.delegated(source, field, value)), source
                    )
                    self.assertNotIn("compaction just squashed", context)
                    self.assertNotIn("type: handoff", context)
                    self.assertNotIn("dispatch `kb-librarian`", context)
                    self.assertNotIn("brief `kb-scribe`", context)
                    self.assertNotIn("SendMessage", context)
                    # What it does get: the inline-work pointer and the one body rule.
                    self.assertIn("happens inline", context)
                    self.assertIn("references/authoring.md", context)
                    self.assertIn("NEVER inject gitian markup", context)

    def test_no_source_profile_tail_and_no_stamping_for_the_parent_session(self):
        # The resume tail points at kb-librarian, and stamping would record the PARENT's session
        # (a subagent's hooks run under it) as having seen a vocabulary nobody read.
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 9, "topics": [_topic("kb", "d", 1)]}},
            sessions={"sess-sub": _session_defaults(lastSeenVocabRev=_last_seen(3))},
        )
        context = self.context_of(self.run_hook(self.delegated("resume")), "resume")
        self.assertNotIn("vocab_rev", context)
        self.assertNotIn("kb-librarian for a vocab-delta", context)
        stamped = self.dump_state()["sessions"]["sess-sub"]["lastSeenVocabRev"][SERVER_KEY]["home"]
        self.assertEqual(stamped, 3)

    def test_the_primary_still_gets_the_compact_handoff(self):
        context = self.context_of(self.run_hook(self.envelope("compact")), "compact")
        self.assertIn("compaction just squashed", context)


class TranscriptLines(SessionContextTestCase):
    def test_transcript_and_extractor_lines_are_emitted_when_the_file_exists(self):
        path = self.write_transcript()
        context = self.context_of(
            self.run_hook(self.envelope("startup", transcript_path=path)), "startup"
        )
        self.assertIn("- transcript: %s" % path, context)
        self.assertIn(
            "- transcript-extract: python3 %s" % (HOOKS_DIR / "transcript_extract.py"), context
        )

    def test_the_extractor_command_actually_runs(self):
        # The injected command is the scribe's whole path to the conversation -- a path that
        # doesn't resolve is worse than no line at all.
        path = self.write_transcript()
        context = self.context_of(
            self.run_hook(self.envelope("startup", transcript_path=path)), "startup"
        )
        command = context.split("- transcript-extract: ")[1].split("\n")[0]
        parts = command.split(" ")
        proc = subprocess.run(
            [sys.executable, parts[1], path], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertIn("## user", proc.stdout)

    def test_both_lines_are_omitted_when_no_transcript_path_is_supplied(self):
        context = self.context_of(self.run_hook(self.envelope("startup")), "startup")
        self.assertNotIn("- transcript:", context)
        self.assertNotIn("transcript-extract", context)

    def test_both_lines_are_omitted_when_the_transcript_does_not_exist(self):
        missing = os.path.join(self.tmpdir, "gone.jsonl")
        context = self.context_of(
            self.run_hook(self.envelope("resume", transcript_path=missing)), "resume"
        )
        self.assertNotIn("- transcript:", context)
        self.assertNotIn("transcript-extract", context)


class NoVocabDigest(SessionContextTestCase):
    def test_startup_never_emits_a_digest_even_with_a_full_cache(self):
        self.seed_state(
            servers={
                SERVER_KEY: {
                    "vocabRev": 4,
                    "vocabFetchedAt": "2026-07-18T10:00:00Z",
                    "topics": [_topic("auth", "Authentication flows", 5), _topic("billing", "", 2)],
                }
            }
        )
        context = self.context_of(self.run_hook(self.envelope("startup")), "startup")
        self.assertNotIn("KB vocab digest", context)
        self.assertNotIn("Authentication flows", context)
        self.assertIn("kb-scribe", context)  # the static context is intact

    def test_clear_still_bumps_the_epoch(self):
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 2, "topics": [_topic("kb", "Knowledge base", 3)]}},
            sessions={"sess-clear": _session_defaults(epoch=0, flags={"orientation": True})},
        )
        context = self.context_of(
            self.run_hook(self.envelope("clear", session_id="sess-clear")), "clear"
        )
        self.assertNotIn("KB vocab digest", context)

        state = self.dump_state()
        session = state["sessions"]["sess-clear"]
        self.assertEqual(session["epoch"], 1)
        self.assertEqual(session["flags"], {})

    def test_compact_briefs_the_scribe_for_a_handoff(self):
        context = self.context_of(self.run_hook(self.envelope("compact")), "compact")
        self.assertIn("compaction just squashed", context)
        self.assertIn("type: handoff", context)
        self.assertIn("kb-scribe", context)

    def test_every_source_stamps_last_seen_vocab_rev(self):
        # The digest is gone but the stamping is not: harvest.py and the resume delta line both
        # read this bucket, so a session that skipped it would report a stale "vocab moved".
        self.seed_state(servers={SERVER_KEY: {"vocabRev": 6, "topics": [_topic("kb", "d", 1)]}})
        self.run_hook(self.envelope("startup", session_id="sess-stamp"))
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-stamp"]["lastSeenVocabRev"][SERVER_KEY]["home"], 6)


class ResumeProfile(SessionContextTestCase):
    def test_unmoved_vocab_rev_adds_no_delta_line(self):
        recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 5, "topics": [_topic("kb", "d", 1)]}},
            sessions={
                "sess-r1": _session_defaults(lastSeenVocabRev=_last_seen(5), updatedAt=recent)
            },
        )
        context = self.context_of(
            self.run_hook(self.envelope("resume", session_id="sess-r1")), "resume-unmoved"
        )
        self.assertNotIn("KB vocabulary moved", context)
        self.assertNotIn("running record", context)

    def test_moved_vocab_rev_adds_exactly_one_delta_line_pointing_at_the_librarian(self):
        recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 9, "topics": [_topic("kb", "d", 1)]}},
            sessions={
                "sess-r2": _session_defaults(lastSeenVocabRev=_last_seen(5), updatedAt=recent)
            },
        )
        context = self.context_of(
            self.run_hook(self.envelope("resume", session_id="sess-r2")), "resume-moved"
        )
        self.assertEqual(context.count("KB vocabulary moved"), 1)
        self.assertIn("vocab_rev 5 -> 9", context)
        # Reworded for delegation: the primary no longer reads the vocab resource itself.
        self.assertIn("kb-librarian", context.split("KB vocabulary moved")[1])
        self.assertNotIn("re-read gitian-kb://vocab", context)

    def test_stale_session_adds_staleness_line(self):
        stale = _iso(datetime.now(timezone.utc) - timedelta(hours=13))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 3, "topics": [_topic("kb", "d", 1)]}},
            sessions={
                "sess-r3": _session_defaults(lastSeenVocabRev=_last_seen(3), updatedAt=stale)
            },
        )
        context = self.context_of(
            self.run_hook(self.envelope("resume", session_id="sess-r3")), "resume-stale"
        )
        self.assertNotIn("KB vocabulary moved", context)  # vocab unmoved
        self.assertIn("running record", context)

    def test_no_prior_baseline_stays_silent_on_delta(self):
        # lastSeenVocabRev has no bucket for this (server, kb) yet -- nothing to say "moved" from.
        recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 9, "topics": [_topic("kb", "d", 1)]}},
            sessions={"sess-r4": _session_defaults(lastSeenVocabRev={}, updatedAt=recent)},
        )
        context = self.context_of(
            self.run_hook(self.envelope("resume", session_id="sess-r4")), "resume-no-baseline"
        )
        self.assertNotIn("KB vocabulary moved", context)

    def test_resume_never_bumps_epoch_and_flags_survive(self):
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 3, "topics": [_topic("kb", "d", 1)]}},
            sessions={
                "sess-r5": _session_defaults(
                    epoch=2, lastSeenVocabRev=_last_seen(3), flags={"orientation": True}
                )
            },
        )
        self.run_hook(self.envelope("resume", session_id="sess-r5"))
        self.run_hook(self.envelope("resume", session_id="sess-r5"))

        state = self.dump_state()
        session = state["sessions"]["sess-r5"]
        self.assertEqual(session["epoch"], 2)
        self.assertTrue(session["flags"]["orientation"])

    def test_resume_stamps_last_seen_vocab_rev_to_cache(self):
        recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 9, "topics": [_topic("kb", "d", 1)]}},
            sessions={
                "sess-r6": _session_defaults(lastSeenVocabRev=_last_seen(5), updatedAt=recent)
            },
        )
        self.run_hook(self.envelope("resume", session_id="sess-r6"))
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-r6"]["lastSeenVocabRev"][SERVER_KEY]["home"], 9)


class LedgerCarryOver(SessionContextTestCase):
    """The activity ledger (ledger.py) at SessionStart: a session coming back (resume), squashed
    (compact) or about to be emptied (clear) is told what no KB write covers yet, and only when
    something qualifying is really open. startup says nothing of it, and a delegated session hears
    nothing and changes nothing."""

    SID = "sess-l"
    TRANSCRIPT = "/tmp/gks-session-transcript.jsonl"  # never opened, only named in the text

    def seed_open(self, **fields):
        """A session with one commit three hours old that nothing covers."""
        self.seed_state(sessions={self.SID: _ledger_session(events=[_commit(180)], **fields)})

    def session(self):
        return self.dump_state()["sessions"][self.SID]

    def start(self, source, **payload_extra):
        payload = self.envelope(source, session_id=self.SID)
        payload.update(payload_extra)
        return self.context_of(self.run_hook(payload), source)

    def assert_just_now(self, stamp):
        parsed = ledger_mod.parse_iso(stamp)
        self.assertIsNotNone(parsed, msg="not a timestamp: %r" % (stamp,))
        self.assertLess(abs((datetime.now(timezone.utc) - parsed).total_seconds()), 120)

    def test_resume_lists_the_open_activity_and_asks_for_one_brief(self):
        self.seed_open()
        context = self.start("resume")
        head = context.index("gitian-kb: resumed with activity since ")
        line = context.index("- acme/api: 1 commit")
        tail = context.index(
            "If any of it clears the bar, brief kb-scribe (background) from this list."
        )
        self.assertLess(head, line)
        self.assertLess(line, tail)
        self.assertIn("that no KB write covers yet:\n- acme/api", context)
        self.assertIn("abc1234", context)
        self.assertNotIn("before /clear", context)
        self.assertNotIn("fold it into that handoff brief", context)

        session = self.session()
        self.assert_just_now(session["nudges"]["at"])
        self.assertEqual(len(session["ledger"]["events"]), 1)  # resume reports, it never resets

    def test_compact_lists_it_right_after_the_handoff_directive(self):
        self.seed_open()
        context = self.start("compact")
        directive = context.index("type: handoff")
        head = context.index("Activity since ")
        self.assertLess(directive, head)
        self.assertIn(
            " that no KB write covers yet -- fold it into that handoff brief:"
            "\n- acme/api: 1 commit",
            context,
        )
        self.assertNotIn("If any of it clears the bar", context)
        self.assertNotIn("before /clear", context)

        session = self.session()
        self.assert_just_now(session["nudges"]["at"])
        self.assertEqual(len(session["ledger"]["events"]), 1)

    def test_clear_names_it_once_with_the_transcript_and_starts_the_ledger_over(self):
        self.seed_open(
            watch=[_watch_entry()],
            transcript=self.TRANSCRIPT,
            scribeIds=["scribe-id-1"],
            briefAt=_ago(minutes=100),  # long past its grace: the commit is still owed
            dropped=3,
        )
        context = self.start("clear")
        self.assertIn("gitian-kb: before /clear, this session left activity since ", context)
        self.assertIn(" that no KB write covers:\n- acme/api: 1 commit", context)
        self.assertIn(
            "If any of it matters, brief kb-scribe (background) with the transcript %s."
            % self.TRANSCRIPT,
            context,
        )

        session = self.session()
        ledger = session["ledger"]
        self.assertEqual(ledger["events"], [])
        self.assertEqual(ledger["edits"], [])
        self.assertEqual(ledger["dropped"], 0)
        self.assertNotIn("briefAt", ledger)
        self.assert_just_now(ledger["checkpointAt"])
        # What survives: the scribes this session knows, the transcript, and the pull requests
        # being watched -- one open before /clear is still open after it.
        self.assertEqual(ledger["scribeIds"], ["scribe-id-1"])
        self.assertEqual(ledger["transcript"], self.TRANSCRIPT)
        self.assertEqual([(w["repo"], w["number"]) for w in session["watch"]], [("acme/web", 7)])
        self.assert_just_now(session["nudges"]["at"])
        self.assertEqual(session["epoch"], 1)  # the epoch bump still runs first

        # Said once: the ledger starts over, so a second /clear has nothing left to name.
        self.assertNotIn("before /clear", self.start("clear"))

    def test_clear_without_a_recorded_transcript_drops_that_clause(self):
        self.seed_open()
        context = self.start("clear")
        self.assertIn("If any of it matters, brief kb-scribe (background).", context)
        self.assertNotIn("with the transcript", context)

    def test_nothing_open_means_nothing_said_and_nothing_changed(self):
        stamp = _ago(minutes=9)
        two_edits = [{"min": stamp[:16], "at": stamp, "n": 2, "repo": "acme/api"}]
        quiet = {
            "an empty ledger": {},
            "covered by a write": {"events": [_commit(180)], "checkpointAt": _ago(minutes=5)},
            "covered by a brief": {"events": [_commit(20)], "briefAt": _ago(minutes=5)},
            "information only": {
                "events": [
                    {"at": _ago(minutes=30), "kind": "pr", "repo": "acme/api", "ref": "#7"},
                    {"at": _ago(minutes=20), "kind": "ruling", "text": "Never merge with --auto."},
                    {"at": _ago(minutes=10), "kind": "release", "repo": "acme/api", "ref": "v1",
                     "draft": True},
                ],
                "edits": two_edits,
            },
        }
        for label, fields in quiet.items():
            for source in ("resume", "compact", "clear"):
                with self.subTest(ledger=label, source=source):
                    self.seed_state(sessions={self.SID: _ledger_session(**fields)})
                    before = self.session()
                    context = self.start(source)
                    for fragment in ("no KB write covers", "fold it into", "before /clear"):
                        self.assertNotIn(fragment, context)
                    after = self.session()
                    self.assertEqual(after["ledger"], before["ledger"])  # clear did not reset it
                    self.assertEqual(after["nudges"], {})

    def test_startup_and_unknown_sources_say_nothing_of_it(self):
        for source in ("startup", "some-future-source"):
            with self.subTest(source=source):
                self.seed_open()
                before = self.session()
                context = self.start(source)
                for fragment in ("no KB write covers", "fold it into", "before /clear"):
                    self.assertNotIn(fragment, context)
                after = self.session()
                self.assertEqual(after["ledger"], before["ledger"])
                self.assertEqual(after["nudges"], {})

    def test_the_vocab_and_staleness_lines_survive_beside_it(self):
        session = _ledger_session(events=[_commit(13 * 60)])
        session.update(lastSeenVocabRev=_last_seen(5), updatedAt=_ago(hours=13))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 9, "topics": [_topic("kb", "d", 1)]}},
            sessions={self.SID: session},
        )
        context = self.start("resume")
        self.assertIn("vocab_rev 5 -> 9", context)
        self.assertIn("running record", context)
        self.assertLess(
            context.index("resumed with activity since"), context.index("KB vocabulary moved")
        )

    def test_a_delegated_session_hears_nothing_and_changes_nothing(self):
        for field, value in (("agent_id", "agent-123"), ("agent_type", "kb-scribe")):
            for source in ("startup", "resume", "clear", "compact"):
                with self.subTest(field=field, source=source):
                    self.seed_open(watch=[_watch_entry()])
                    before = self.session()
                    context = self.start(source, **{field: value})
                    for fragment in ("no KB write covers", "fold it into", "before /clear"):
                        self.assertNotIn(fragment, context)
                    after = self.session()
                    for key in ("ledger", "nudges", "watch"):
                        self.assertEqual(after[key], before[key], msg=key)

    def test_the_nudge_mark_leaves_the_next_checkpoint_only_what_is_new(self):
        self.seed_open()
        self.start("resume")
        session = self.session()
        now = ledger_mod.utcnow()
        self.assertTrue(ledger_mod.has_open_qualifying(session, now))  # still owed ...
        # ... but not announced again.
        self.assertFalse(ledger_mod.checkpoint_due(session, now, at_prompt=True))

    def test_text_from_git_cannot_break_the_json(self):
        # Commit subjects and PR titles are somebody else's text; a backslash or a quote in one
        # must reach the model intact (or tamed) and never corrupt the hook's JSON.
        nasty = 'fix C:\\temp\\new "quoted" \\n literal'
        self.seed_state(
            sessions={
                self.SID: _ledger_session(
                    events=[_commit(180, text=nasty)], transcript='/tmp/we"ird\\path.jsonl'
                )
            }
        )
        context = self.start("clear")  # context_of parses the whole output as JSON
        self.assertIn(nasty.replace('"', "'"), context)
        self.assertIn('/tmp/we"ird\\path.jsonl', context)


class PinnedLiteralsAndRobustness(SessionContextTestCase):
    def test_every_source_is_valid_json_with_pinned_literals(self):
        self.seed_state(servers={SERVER_KEY: {"vocabRev": 1, "topics": [_topic("kb", "d", 1)]}})
        for source in ("startup", "resume", "clear", "compact", "totally-unknown-source"):
            proc = self.run_hook(self.envelope(source, session_id="sess-pinned-%s" % source))
            self.assertEqual(proc.returncode, 0, msg="source=%r stderr=%r" % (source, proc.stderr))
            for literal in PINNED_RUNTIME_LITERALS:
                self.assertIn(literal, proc.stdout, "missing %r for source=%r" % (literal, source))
            json.loads(proc.stdout)  # must parse

    def test_unknown_source_behaves_like_startup(self):
        self.seed_state(servers={SERVER_KEY: {"vocabRev": 1, "topics": [_topic("kb", "d", 1)]}})
        context = self.context_of(self.run_hook(self.envelope("some-future-source")), "unknown")
        self.assertNotIn("compaction just squashed", context)  # not the compact tail
        self.assertIn("kb-scribe", context)

    def test_empty_stdin_still_emits_valid_context_json(self):
        proc = self.run_hook("")
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        context = self.context_of(proc, "empty-stdin")
        self.assertIn("kb-scribe", context)
        self.assertIn("NEVER inject gitian markup", context)

    def test_garbage_stdin_still_emits_valid_context_json(self):
        proc = self.run_hook("{not valid json ][ at all")
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        context = self.context_of(proc, "garbage-stdin")
        self.assertIn("kb-librarian", context)

    def test_corrupt_state_file_falls_back_to_static_context_silently(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")

        proc = self.run_hook(self.envelope("startup"))
        context = self.context_of(proc, "corrupt-state")
        self.assertIn("kb-scribe", context)

    def test_a_transcript_path_with_a_quote_cannot_break_the_json(self):
        # Paths are interpolated into a JSON string; an unescaped `"` would drop the whole
        # context injection silently.
        weird = os.path.join(self.tmpdir, 'we"ird.jsonl')
        with open(weird, "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        proc = self.run_hook(self.envelope("startup", transcript_path=weird))
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        json.loads(proc.stdout)  # must still parse


class OpenPullRequestLine(SessionContextTestCase):
    """The one `gh pr view` the hook makes: it names the branch's OPEN pull request and nothing
    else, and every way it can go wrong leaves the line out without touching the rest of the
    context. A fake `gh` earlier on PATH stands in for the real one, so no test needs a network, a
    login or this checkout's own branch."""

    BRANCH = "feat/pr-refs"
    OPEN = '{"number":12,"state":"OPEN","url":"https://github.com/acme/api/pull/12"}'

    def setUp(self):
        super().setUp()
        self.checkout = os.path.join(self.tmpdir, "checkout")
        os.makedirs(self.checkout)
        self.env["CLAUDE_PROJECT_DIR"] = self.checkout
        self.bin_dir = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.bin_dir)
        self.env["PATH"] = self.bin_dir + os.pathsep + self.env.get("PATH", "")
        self.calls = os.path.join(self.tmpdir, "gh-calls.txt")

    def git_init(self):
        subprocess.run(
            ["git", "init", "-q", "-b", self.BRANCH, self.checkout],
            check=True,
            capture_output=True,
            env=self.env,
        )

    def fake_gh(self, body):
        path = os.path.join(self.bin_dir, "gh")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\necho "$@" >> "%s"\n%s\n' % (self.calls, body))
        os.chmod(path, 0o755)

    def fake_gh_printing(self, payload):
        self.fake_gh("printf '%%s' '%s'" % payload)

    def context(self):
        return self.context_of(self.run_hook(self.envelope("startup")), "startup")

    def gh_was_called(self):
        return os.path.exists(self.calls)

    def assert_no_line_and_context_intact(self, context):
        self.assertNotIn("open PR", context)
        self.assertIn("- branch: %s" % self.BRANCH, context)
        self.assertIn("kb-scribe", context)

    def test_an_open_pr_is_named_with_its_number_and_url(self):
        # Regression: without the line a brief has no PR to carry, and the scribe is left to guess
        # one out of the branch name.
        self.git_init()
        self.fake_gh_printing(self.OPEN)
        context = self.context()
        self.assertIn(
            "- open PR for this branch: #12 https://github.com/acme/api/pull/12", context
        )
        self.assertIn("- branch: %s" % self.BRANCH, context)
        with open(self.calls, encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "pr view --json number,url,state")

    def test_a_merged_or_closed_pr_is_not_called_the_branchs_open_pr(self):
        # Regression: `gh pr view` answers for a merged PR too; naming it as the open one would
        # send the next brief at a PR that has already landed.
        self.git_init()
        for state in ("MERGED", "CLOSED"):
            with self.subTest(state=state):
                self.fake_gh_printing(
                    '{"number":12,"state":"%s","url":"https://github.com/acme/api/pull/12"}' % state
                )
                self.assert_no_line_and_context_intact(self.context())

    def test_a_failing_gh_leaves_the_line_out_silently(self):
        # No PR for the branch, no login, no network: gh exits non-zero. The hook must not
        # surface the error or lose the rest of the context.
        self.git_init()
        self.fake_gh("echo 'no pull requests found for branch' >&2; exit 1")
        proc = self.run_hook(self.envelope("startup"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assert_no_line_and_context_intact(self.context_of(proc, "startup"))

    def test_unparseable_or_hostile_gh_output_is_dropped(self):
        # Regression: the URL is interpolated into a JSON string, so a quote in it would drop the
        # whole context injection. Anything that is not `#N https://…` of plain URL characters is
        # not shown.
        self.git_init()
        for payload in (
            "not json at all",
            '{"number":"x","state":"OPEN","url":"https://github.com/a/b/pull/1"}',
            '{"number":3,"state":"OPEN","url":"https://x.test/a\\"b"}',
            '{"number":3,"state":"OPEN","url":"javascript:alert(1)"}',
            '{"number":3,"state":"OPEN","url":"https://x.test/a b"}',
        ):
            with self.subTest(payload=payload):
                self.fake_gh_printing(payload)
                proc = self.run_hook(self.envelope("startup"))
                self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
                json.loads(proc.stdout)  # must still parse
                self.assert_no_line_and_context_intact(self.context_of(proc, "startup"))

    @unittest.skipUnless(
        shutil.which("timeout") or shutil.which("gtimeout"), "needs a timeout binary"
    )
    def test_a_hanging_gh_is_cut_off_and_the_hook_still_answers(self):
        # Regression: SessionStart blocks on this hook; a gh waiting on a dead network must cost
        # a few seconds at most, never the session. (`exec` so the process that is killed is the
        # sleep itself and no orphan holds the pipe open.)
        self.git_init()
        self.fake_gh("exec sleep 30")
        started = time.monotonic()
        proc = self.run_hook(self.envelope("startup"))
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 8, msg="hook took %.1fs with a hung gh" % elapsed)
        self.assert_no_line_and_context_intact(self.context_of(proc, "startup"))

    def test_outside_a_git_repo_gh_is_never_run(self):
        # The checkout is a bare directory: no branch, so no network call for nothing.
        self.fake_gh_printing(self.OPEN)
        context = self.context()
        self.assertNotIn("open PR", context)
        self.assertFalse(self.gh_was_called())

    def test_a_detached_head_has_no_branch_and_so_no_gh_call(self):
        self.git_init()
        git_env = dict(self.env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.test",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.test")
        empty_tree = subprocess.run(
            ["git", "-C", self.checkout, "hash-object", "-t", "tree", "-w", "--stdin"],
            input="", capture_output=True, text=True, check=True, env=git_env,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "-C", self.checkout, "commit-tree", empty_tree, "-m", "x"],
            capture_output=True, text=True, check=True, env=git_env,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", self.checkout, "update-ref", "--no-deref", "HEAD", commit],
            check=True, capture_output=True, env=git_env,
        )
        self.fake_gh_printing(self.OPEN)
        context = self.context()
        self.assertNotIn("open PR", context)
        self.assertNotIn("- branch:", context)
        self.assertFalse(self.gh_was_called())

    def test_the_line_reaches_a_delegated_session_too(self):
        # It is a derived line like repo and branch, which a subagent gets as well.
        self.git_init()
        self.fake_gh_printing(self.OPEN)
        payload = self.envelope("startup", session_id="sess-sub")
        payload["agent_id"] = "agent-123"
        context = self.context_of(self.run_hook(payload), "startup")
        self.assertIn("- open PR for this branch: #12 ", context)

    # --- the watch: the open PR is polled later, because a merge leaves no local command ------

    def watch_of(self, sid):
        return self.dump_state().get("sessions", {}).get(sid, {}).get("watch", [])

    def test_the_open_pr_goes_on_the_watch_on_every_source(self):
        self.git_init()
        self.fake_gh_printing(self.OPEN)
        for source in ("startup", "resume", "clear", "compact"):
            with self.subTest(source=source):
                sid = "sess-w-%s" % source
                self.context_of(self.run_hook(self.envelope(source, session_id=sid)), source)
                [entry] = self.watch_of(sid)
                self.assertEqual(
                    (entry["repo"], entry["number"], entry["why"]), ("acme/api", 12, "branch")
                )
                self.assertIsNotNone(ledger_mod.parse_iso(entry["addedAt"]))
                self.assertNotIn("polledAt", entry)  # never polled yet: the first Stop will

    def test_a_pr_already_on_the_watch_is_not_added_again(self):
        self.git_init()
        self.fake_gh_printing(self.OPEN)
        self.context()
        self.context()
        self.assertEqual(len(self.watch_of("sess-1")), 1)

    def test_no_open_pr_means_no_watch_and_no_state_write(self):
        self.git_init()
        for state in ("MERGED", "CLOSED"):
            self.fake_gh_printing(
                '{"number":12,"state":"%s","url":"https://github.com/acme/api/pull/12"}' % state
            )
            self.context()
        self.fake_gh("exit 1")
        self.context()
        self.assertFalse(os.path.exists(self.state_file))

    def test_only_a_github_pull_request_url_is_watched(self):
        self.git_init()
        for payload in (
            '{"number":3,"state":"OPEN","url":"https://x.test/acme/api/pull/3"}',
            '{"number":3,"state":"OPEN","url":"https://x.test/a b"}',
            '{"number":3,"state":"OPEN","url":"javascript:alert(1)"}',
            "not json at all",
        ):
            with self.subTest(payload=payload):
                self.fake_gh_printing(payload)
                self.context_of(self.run_hook(self.envelope("startup")), "startup")
                self.assertFalse(os.path.exists(self.state_file))

    def test_a_delegated_session_puts_nothing_on_the_watch(self):
        self.git_init()
        self.fake_gh_printing(self.OPEN)
        payload = self.envelope("startup", session_id="sess-sub")
        payload["agent_id"] = "agent-123"
        self.context_of(self.run_hook(payload), "startup")
        self.assertFalse(os.path.exists(self.state_file))

    def test_the_watch_and_the_ledger_text_arrive_together(self):
        self.git_init()
        self.fake_gh_printing(self.OPEN)
        self.seed_state(sessions={"sess-1": _ledger_session(events=[_commit(180)])})
        context = self.context_of(self.run_hook(self.envelope("resume")), "resume")
        self.assertIn("resumed with activity since", context)
        session = self.dump_state()["sessions"]["sess-1"]
        self.assertEqual([(w["repo"], w["number"]) for w in session["watch"]], [("acme/api", 12)])
        self.assertIn("at", session["nudges"])


if __name__ == "__main__":
    unittest.main()
