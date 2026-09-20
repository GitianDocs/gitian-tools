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
which is clearer written as a literal dict than composed through incr/merge calls.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_session_context.py
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
SESSION_CONTEXT_SH = HOOKS_DIR / "session-context.sh"
STATE_PY = HOOKS_DIR / "state.py"

SERVER_KEY = "https://gitian.dev/api/mcp"  # default GITIAN_KB_URL, per the state contract

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


if __name__ == "__main__":
    unittest.main()
