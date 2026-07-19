#!/usr/bin/env python3
"""Unit tests for session-context.sh + session_digest.py, the gitian-kb nudge layer's SessionStart
source-profile hook.

Drives the hook end to end via `sh session-context.sh` (matching how hooks.json actually invokes
it), with GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other. State is seeded by writing a
full v1-shaped JSON document directly to that path (same approach as test_state.py's
SevenDayPruning fixture) rather than shelling out to state.py -- these tests care about exact
seed shapes (a specific vocabRev/lastSeenVocabRev pairing, a specific updatedAt age), which is
clearer written as a literal dict than composed through incr/merge calls.

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

# Pinned literals every SessionStart source must reproduce verbatim (see
# src/lib/kb/plugin-contract.test.ts and the plan's "honor verbatim" list).
PINNED_RUNTIME_LITERALS = (
    '"hookEventName":"SessionStart"',
    "additionalContext",
    "gitian-kb://format/",
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
        "lastSeenVocabRev": None,
        "lintHashes": [],
        "mintPrompted": [],
        "updatedAt": _iso(datetime.now(timezone.utc)),
    }
    session.update(overrides)
    return session


def _topic(slug, description="", degree=1):
    return {"slug": slug, "description": description, "degree": degree}


class SessionContextTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-session-context-test-")
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env.pop("GITIAN_KB_URL", None)

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

    def envelope(self, source, session_id="sess-1"):
        return {
            "session_id": session_id,
            "transcript_path": "/tmp/transcript.jsonl",
            "cwd": "/repo",
            "hook_event_name": "SessionStart",
            "source": source,
        }


class Digest(SessionContextTestCase):
    def test_startup_emits_digest_when_cache_has_topics(self):
        self.seed_state(
            servers={
                SERVER_KEY: {
                    "vocabRev": 4,
                    "vocabFetchedAt": "2026-07-18T10:00:00Z",
                    "topics": [
                        _topic("auth", "Authentication flows", 5),
                        _topic("billing", "", 2),  # empty description
                    ],
                }
            }
        )
        proc = self.run_hook(self.envelope("startup"))
        context = self.context_of(proc, "startup")

        self.assertIn("KB vocab digest (as of vocab_rev 4, harvested 2026-07-18T10:00:00Z):", context)
        self.assertIn("- auth - Authentication flows (degree 5)", context)
        self.assertIn("- billing - (no description yet) (degree 2)", context)

    def test_startup_omits_digest_cleanly_when_no_topics(self):
        proc = self.run_hook(self.envelope("startup"))  # no state file at all
        context = self.context_of(proc, "startup-empty")

        self.assertNotIn("KB vocab digest", context)
        # The static context must still be intact.
        self.assertIn("RAG discipline", context)

    def test_startup_omits_digest_when_server_has_empty_topics_list(self):
        self.seed_state(servers={SERVER_KEY: {"vocabRev": 1, "topics": []}})
        proc = self.run_hook(self.envelope("startup"))
        context = self.context_of(proc, "startup-empty-list")
        self.assertNotIn("KB vocab digest", context)

    def test_digest_caps_at_25_and_notes_remainder(self):
        topics = [_topic("t%02d" % i, "d", i) for i in range(30)]
        self.seed_state(servers={SERVER_KEY: {"vocabRev": 1, "topics": topics}})
        proc = self.run_hook(self.envelope("startup"))
        context = self.context_of(proc, "startup-capped")

        self.assertEqual(context.count("- t"), 25)
        self.assertIn("...and 5 more", context)

    def test_clear_bumps_epoch_and_reemits_digest(self):
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 2, "topics": [_topic("kb", "Knowledge base", 3)]}},
            sessions={"sess-clear": _session_defaults(epoch=0, flags={"orientation": True})},
        )
        proc = self.run_hook(self.envelope("clear", session_id="sess-clear"))
        context = self.context_of(proc, "clear")

        self.assertIn("KB vocab digest", context)
        self.assertIn("- kb - Knowledge base (degree 3)", context)

        state = self.dump_state()
        session = state["sessions"]["sess-clear"]
        self.assertEqual(session["epoch"], 1)
        self.assertEqual(session["flags"], {})

    def test_compact_retains_handoff_directive(self):
        proc = self.run_hook(self.envelope("compact"))
        context = self.context_of(proc, "compact")
        self.assertIn(
            "distill the pre-compact work into the KB as a handoff", context
        )


class ResumeProfile(SessionContextTestCase):
    def test_unmoved_vocab_rev_adds_no_delta_line(self):
        recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 5, "topics": [_topic("kb", "d", 1)]}},
            sessions={"sess-r1": _session_defaults(lastSeenVocabRev=5, updatedAt=recent)},
        )
        proc = self.run_hook(self.envelope("resume", session_id="sess-r1"))
        context = self.context_of(proc, "resume-unmoved")

        self.assertNotIn("KB vocabulary moved", context)
        self.assertNotIn("running record", context)
        self.assertNotIn("KB vocab digest", context)  # never a digest on resume

    def test_moved_vocab_rev_adds_exactly_one_delta_line(self):
        recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 9, "topics": [_topic("kb", "d", 1)]}},
            sessions={"sess-r2": _session_defaults(lastSeenVocabRev=5, updatedAt=recent)},
        )
        proc = self.run_hook(self.envelope("resume", session_id="sess-r2"))
        context = self.context_of(proc, "resume-moved")

        self.assertEqual(context.count("KB vocabulary moved"), 1)
        self.assertIn("vocab_rev 5 -> 9", context)
        self.assertIn("gitian-kb://vocab", context)
        self.assertNotIn("running record", context)
        self.assertNotIn("KB vocab digest", context)

    def test_stale_session_adds_staleness_line(self):
        stale = _iso(datetime.now(timezone.utc) - timedelta(hours=13))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 3, "topics": [_topic("kb", "d", 1)]}},
            sessions={"sess-r3": _session_defaults(lastSeenVocabRev=3, updatedAt=stale)},
        )
        proc = self.run_hook(self.envelope("resume", session_id="sess-r3"))
        context = self.context_of(proc, "resume-stale")

        self.assertNotIn("KB vocabulary moved", context)  # vocab unmoved
        self.assertIn("running record", context)

    def test_no_prior_baseline_stays_silent_on_delta(self):
        # lastSeenVocabRev is None -- nothing to say "moved" from.
        recent = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 9, "topics": [_topic("kb", "d", 1)]}},
            sessions={"sess-r4": _session_defaults(lastSeenVocabRev=None, updatedAt=recent)},
        )
        proc = self.run_hook(self.envelope("resume", session_id="sess-r4"))
        context = self.context_of(proc, "resume-no-baseline")
        self.assertNotIn("KB vocabulary moved", context)

    def test_resume_never_bumps_epoch_and_flags_survive(self):
        self.seed_state(
            servers={SERVER_KEY: {"vocabRev": 3, "topics": [_topic("kb", "d", 1)]}},
            sessions={
                "sess-r5": _session_defaults(epoch=2, lastSeenVocabRev=3, flags={"orientation": True})
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
            sessions={"sess-r6": _session_defaults(lastSeenVocabRev=5, updatedAt=recent)},
        )
        self.run_hook(self.envelope("resume", session_id="sess-r6"))
        state = self.dump_state()
        self.assertEqual(state["sessions"]["sess-r6"]["lastSeenVocabRev"], 9)


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
        proc = self.run_hook(self.envelope("some-future-source"))
        context = self.context_of(proc, "unknown-source")
        self.assertIn("KB vocab digest", context)

    def test_empty_stdin_still_emits_valid_context_json(self):
        proc = self.run_hook("")
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        context = self.context_of(proc, "empty-stdin")
        self.assertIn("RAG discipline", context)
        self.assertIn("NEVER inject gitian markup", context)

    def test_garbage_stdin_still_emits_valid_context_json(self):
        proc = self.run_hook("{not valid json ][ at all")
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        context = self.context_of(proc, "garbage-stdin")
        self.assertIn("RAG discipline", context)

    def test_corrupt_state_file_falls_back_to_static_context_silently(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")

        proc = self.run_hook(self.envelope("startup"))
        context = self.context_of(proc, "corrupt-state")
        self.assertIn("RAG discipline", context)
        self.assertNotIn("KB vocab digest", context)


if __name__ == "__main__":
    unittest.main()
