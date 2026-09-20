#!/usr/bin/env python3
"""Unit tests for transcript_extract.py, the gitian-kb plugin's session-transcript reducer.

Drives it end to end as a CLI (`python3 transcript_extract.py <file> [flags]`), which is exactly
how a brief hands it to kb-scribe -- the extractor is not a hook, so there is no stdin envelope
and no state file involved.

Fixtures are SYNTHETIC but shaped from the real Claude Code transcript JSONL: one JSON object per
line, `type` naming the record kind (`user`/`assistant`/`attachment`/`system`/...), the model
message nested under `message.content` as either a bare string (a typed human prompt) or a list of
content blocks (`text`, `thinking`, `tool_use`, `tool_result`, `image`), `isSidechain` marking a
subagent's own conversation, `isMeta` marking hook feedback and local-command envelopes,
`isCompactSummary`/`isVisibleInTranscriptOnly` marking a harness-authored compaction summary, a
`queued_command` attachment carrying a mid-run interjection's `prompt`, and `timestamp` an
ISO-8601 instant with a trailing `Z`. Every shape here was verified against the real transcripts
under ~/.claude/projects/ (record-shape census only). No real transcript content is ever
committed -- the fixture strings below are all invented.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_transcript_extract.py
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
EXTRACT_PY = HOOKS_DIR / "transcript_extract.py"

# The extractor emits in TIMESTAMP order, so a fixture that gave every record the same instant
# would be asserting file order against a tool that does not promise it. Real records carry
# distinct, advancing instants (0 of the kept records in the largest real transcript were missing
# one), so the default here advances too; a test that cares about a specific instant passes it.
_CLOCK = [0]


def _next_timestamp():
    _CLOCK[0] += 1
    return "2026-09-17T10:%02d:%02d.000Z" % divmod(_CLOCK[0], 60)


def user_line(text, timestamp=None, **extra):
    """A typed human prompt: `message.content` is a bare string in this shape."""
    line = {
        "type": "user",
        "isSidechain": False,
        "timestamp": timestamp or _next_timestamp(),
        "message": {"role": "user", "content": text},
    }
    line.update(extra)
    return line


def user_blocks_line(blocks, timestamp=None, **extra):
    line = {
        "type": "user",
        "isSidechain": False,
        "timestamp": timestamp or _next_timestamp(),
        "message": {"role": "user", "content": blocks},
    }
    line.update(extra)
    return line


def assistant_line(blocks, timestamp=None, **extra):
    line = {
        "type": "assistant",
        "isSidechain": False,
        "timestamp": timestamp or _next_timestamp(),
        "message": {"role": "assistant", "content": blocks},
    }
    line.update(extra)
    return line


def text_block(text):
    return {"type": "text", "text": text}


def compact_summary_line(text, timestamp=None, **extra):
    """A compaction summary, as the harness actually writes one. Verified against the real
    transcripts under ~/.claude/projects/: 40 such records across 259 files, every one
    `type: "user"` carrying BOTH `isCompactSummary: true` and `isVisibleInTranscriptOnly: true`,
    with `message.content` a bare string of 17k-36k chars of harness-authored summary. In one
    37 MB transcript those 40 records were 957k chars -- 84% of everything the extractor kept,
    crowding the owner's own words out of --max-chars."""
    line = {
        "type": "user",
        "isSidechain": False,
        "isCompactSummary": True,
        "isVisibleInTranscriptOnly": True,
        "timestamp": timestamp or _next_timestamp(),
        "message": {"role": "user", "content": text},
    }
    line.update(extra)
    return line


def queued_command_line(prompt, timestamp=None, command_mode="prompt", **extra):
    """A mid-run user interjection. A prompt typed while the assistant is still working is
    stored ONLY in this shape -- `{type: "attachment", attachment: {type: "queued_command",
    prompt, commandMode, timestamp}}` -- and (measured: 0 of 107 verbatim) essentially never
    reappears as a `user` record, so an extractor that only reads `user`/`assistant` records
    drops every course correction the owner made mid-turn.

    `commandMode` separates the two populations in the real data: `"prompt"` (13 of 107) is the
    owner's typed text, while `"task-notification"` (94 of 107) is a harness-injected agent
    completion report whose prompt is ENTIRELY one `<task-notification>` envelope -- already an
    INJECTION_TAGS member, so it cleans to empty and drops itself."""
    stamp = timestamp or _next_timestamp()
    line = {
        "type": "attachment",
        "isSidechain": False,
        "timestamp": stamp,
        "attachment": {
            "type": "queued_command",
            "commandMode": command_mode,
            "prompt": prompt,
            "timestamp": stamp,
        },
    }
    line.update(extra)
    return line


class ExtractTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-transcript-extract-test-")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write_transcript(self, lines, name="transcript.jsonl"):
        path = os.path.join(self.tmpdir, name)
        with open(path, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line if isinstance(line, str) else json.dumps(line))
                fh.write("\n")
        return path

    def run_extract(self, *args):
        proc = subprocess.run(
            [sys.executable, str(EXTRACT_PY), *args],
            capture_output=True,
            text=True,
            timeout=30,
        )
        # Fail-open is unconditional: this tool never exits non-zero, whatever it is handed.
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        return proc.stdout

    def blocks_of(self, out):
        """Split stdout into (heading, body) pairs, ignoring a leading truncation banner."""
        pairs = []
        current = None
        for line in out.split("\n"):
            if line in ("## user", "## assistant"):
                if current is not None:
                    pairs.append((current[0], "\n".join(current[1]).strip()))
                current = (line[3:], [])
            elif current is not None:
                current[1].append(line)
        if current is not None:
            pairs.append((current[0], "\n".join(current[1]).strip()))
        return pairs


class TextOnlyExtraction(ExtractTestCase):
    def test_user_string_and_assistant_text_are_emitted_in_order(self):
        path = self.write_transcript(
            [
                user_line("Ship the scribe."),
                assistant_line([text_block("On it -- here is the plan.")]),
                user_line("Good, proceed."),
            ]
        )
        out = self.run_extract(path)
        self.assertEqual(
            self.blocks_of(out),
            [
                ("user", "Ship the scribe."),
                ("assistant", "On it -- here is the plan."),
                ("user", "Good, proceed."),
            ],
        )

    def test_multiple_text_blocks_in_one_message_join(self):
        path = self.write_transcript(
            [assistant_line([text_block("First half."), text_block("Second half.")])]
        )
        out = self.run_extract(path)
        blocks = self.blocks_of(out)
        self.assertEqual(len(blocks), 1)
        self.assertIn("First half.", blocks[0][1])
        self.assertIn("Second half.", blocks[0][1])

    def test_blocks_are_separated_by_a_blank_line(self):
        path = self.write_transcript(
            [user_line("One."), assistant_line([text_block("Two.")])]
        )
        out = self.run_extract(path)
        self.assertEqual(out, "## user\nOne.\n\n## assistant\nTwo.\n")


class ExcludedContent(ExtractTestCase):
    def test_tool_use_thinking_and_tool_result_blocks_are_dropped(self):
        path = self.write_transcript(
            [
                assistant_line(
                    [
                        {"type": "thinking", "thinking": "SECRET-REASONING"},
                        text_block("Reading the file."),
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"file_path": "/etc/TOOL-CALL"},
                        },
                    ]
                ),
                user_blocks_line(
                    [{"type": "tool_result", "content": "TOOL-RESULT-PAYLOAD"}]
                ),
            ]
        )
        out = self.run_extract(path)
        self.assertNotIn("SECRET-REASONING", out)
        self.assertNotIn("TOOL-CALL", out)
        self.assertNotIn("TOOL-RESULT-PAYLOAD", out)
        self.assertEqual(self.blocks_of(out), [("assistant", "Reading the file.")])

    def test_tool_result_only_message_produces_no_block_at_all(self):
        path = self.write_transcript(
            [user_blocks_line([{"type": "tool_result", "content": "x"}])]
        )
        self.assertEqual(self.run_extract(path), "")

    def test_sidechain_subagent_messages_are_excluded(self):
        path = self.write_transcript(
            [
                user_line("Parent prompt."),
                user_line("SUBAGENT-PROMPT", isSidechain=True),
                assistant_line([text_block("SUBAGENT-REPLY")], isSidechain=True),
                assistant_line([text_block("Parent reply.")]),
            ]
        )
        out = self.run_extract(path)
        self.assertNotIn("SUBAGENT", out)
        self.assertEqual(
            self.blocks_of(out), [("user", "Parent prompt."), ("assistant", "Parent reply.")]
        )

    def test_hook_feedback_and_local_command_envelopes_are_excluded(self):
        path = self.write_transcript(
            [
                user_line("Stop hook feedback:\nHOOK-NUDGE-TEXT", isMeta=True),
                user_line(
                    "<local-command-caveat>CAVEAT-TEXT</local-command-caveat>", isMeta=True
                ),
                user_line("<bash-stdout>COMMAND-OUTPUT</bash-stdout><bash-stderr></bash-stderr>"),
                user_line("Real question?"),
            ]
        )
        out = self.run_extract(path)
        self.assertNotIn("HOOK-NUDGE", out)
        self.assertNotIn("CAVEAT-TEXT", out)
        self.assertNotIn("COMMAND-OUTPUT", out)
        self.assertEqual(self.blocks_of(out), [("user", "Real question?")])

    def test_system_reminder_is_stripped_but_the_surrounding_prompt_survives(self):
        path = self.write_transcript(
            [
                user_line(
                    "Do the thing.\n<system-reminder>\nINJECTED-REMINDER\n</system-reminder>"
                )
            ]
        )
        out = self.run_extract(path)
        self.assertNotIn("INJECTED-REMINDER", out)
        self.assertEqual(self.blocks_of(out), [("user", "Do the thing.")])

    def test_non_message_record_types_are_ignored(self):
        path = self.write_transcript(
            [
                {"type": "attachment", "attachment": {"content": "ATTACHMENT-BODY"}},
                {"type": "system", "content": "SYSTEM-BODY", "isSidechain": False},
                {"type": "file-history-snapshot", "snapshot": {"x": "SNAPSHOT-BODY"}},
                user_line("Only me."),
            ]
        )
        out = self.run_extract(path)
        for marker in ("ATTACHMENT-BODY", "SYSTEM-BODY", "SNAPSHOT-BODY"):
            self.assertNotIn(marker, out)
        self.assertEqual(self.blocks_of(out), [("user", "Only me.")])


class CompactionSummaries(ExtractTestCase):
    """A compaction summary is written BY THE HARNESS and stored as a `user` record, so the
    extractor used to emit tens of thousands of characters of machine prose under `## user` --
    text the owner never typed, and (measured) 84% of the budget in a long session."""

    def test_compaction_summary_is_dropped_whole(self):
        path = self.write_transcript(
            [
                user_line("Ship the scribe."),
                compact_summary_line("HARNESS-WRITTEN-SUMMARY of everything so far."),
                user_line("Carry on."),
            ]
        )
        out = self.run_extract(path)
        self.assertNotIn("HARNESS-WRITTEN-SUMMARY", out)
        self.assertEqual(
            self.blocks_of(out), [("user", "Ship the scribe."), ("user", "Carry on.")]
        )

    def test_visible_in_transcript_only_is_dropped_even_without_the_compact_flag(self):
        # No real-user counterexample exists: across 259 real transcripts every
        # isVisibleInTranscriptOnly record was also a compaction summary, so the flag on its own
        # marks harness-rendered chrome rather than anything the owner said.
        path = self.write_transcript(
            [
                user_line("TRANSCRIPT-CHROME", isVisibleInTranscriptOnly=True),
                user_line("Real words."),
            ]
        )
        out = self.run_extract(path)
        self.assertNotIn("TRANSCRIPT-CHROME", out)
        self.assertEqual(self.blocks_of(out), [("user", "Real words.")])

    def test_a_compaction_summary_does_not_consume_the_max_chars_budget(self):
        # The actual harm: the summary is enormous, so under a budget it evicts the real
        # conversation entirely. With it dropped, everything real fits with room to spare.
        path = self.write_transcript(
            [
                user_line("keep-1"),
                compact_summary_line("S" * 20000),
                assistant_line([text_block("keep-2")]),
                user_line("keep-3"),
            ]
        )
        out = self.run_extract(path, "--max-chars", "500")
        self.assertNotIn("[truncated:", out)
        self.assertNotIn("SSSS", out)
        self.assertEqual(
            self.blocks_of(out),
            [("user", "keep-1"), ("assistant", "keep-2"), ("user", "keep-3")],
        )

    def test_a_compaction_summary_does_not_consume_the_last_budget(self):
        path = self.write_transcript(
            [
                user_line("keep-1"),
                compact_summary_line("noise"),
                assistant_line([text_block("keep-2")]),
            ]
        )
        out = self.run_extract(path, "--last", "2")
        self.assertEqual(
            self.blocks_of(out), [("user", "keep-1"), ("assistant", "keep-2")]
        )


class QueuedInterjections(ExtractTestCase):
    """A prompt typed while the assistant is mid-turn is the owner's course correction, and it
    exists in the transcript ONLY as a `queued_command` attachment -- never as a `user` record."""

    def test_queued_command_is_kept_as_a_user_message(self):
        path = self.write_transcript(
            [
                user_line("Do the thing.", timestamp="2026-09-17T10:00:00.000Z"),
                queued_command_line(
                    "Actually, use the patch tools.", timestamp="2026-09-17T10:00:03.000Z"
                ),
            ]
        )
        out = self.run_extract(path)
        self.assertEqual(
            self.blocks_of(out),
            [("user", "Do the thing."), ("user", "Actually, use the patch tools.")],
        )

    def test_interjection_lands_in_timestamp_order_not_file_order(self):
        # The attachment record is appended out of chronological order in real transcripts (68 of
        # 5225 kept records in the largest one), so the interjection has to be placed by its
        # timestamp or it reads as a reply to the answer it was meant to redirect.
        path = self.write_transcript(
            [
                user_line("Start.", timestamp="2026-09-17T10:00:00.000Z"),
                assistant_line(
                    [text_block("Working on it.")], timestamp="2026-09-17T10:00:05.000Z"
                ),
                queued_command_line("Stop -- plan B.", timestamp="2026-09-17T10:00:03.000Z"),
                assistant_line([text_block("Done.")], timestamp="2026-09-17T10:00:10.000Z"),
            ]
        )
        out = self.run_extract(path)
        self.assertEqual(
            self.blocks_of(out),
            [
                ("user", "Start."),
                ("user", "Stop -- plan B."),
                ("assistant", "Working on it."),
                ("assistant", "Done."),
            ],
        )

    def test_harness_task_notification_queued_command_is_not_conversation(self):
        # commandMode "task-notification" is the harness injecting an agent's completion report
        # (94 of 107 real queued commands, every one wholly wrapped in the envelope). It cleans
        # to empty through the existing INJECTION_TAGS pass and drops itself.
        path = self.write_transcript(
            [
                queued_command_line(
                    "<task-notification>AGENT-REPORT-BODY</task-notification>",
                    command_mode="task-notification",
                ),
                user_line("Only me."),
            ]
        )
        out = self.run_extract(path)
        self.assertNotIn("AGENT-REPORT", out)
        self.assertEqual(self.blocks_of(out), [("user", "Only me.")])

    def test_an_enqueue_that_also_landed_as_a_user_record_is_emitted_once(self):
        path = self.write_transcript(
            [
                queued_command_line("Switch to plan B.", timestamp="2026-09-17T10:00:03.000Z"),
                user_line("Switch to plan B.", timestamp="2026-09-17T10:00:07.000Z"),
            ]
        )
        out = self.run_extract(path)
        self.assertEqual(self.blocks_of(out), [("user", "Switch to plan B.")])

    def test_dedup_is_scoped_to_interjections_and_never_collapses_repeated_prompts(self):
        # Two genuinely separate "continue" turns are two messages; only an enqueue/user-record
        # PAIR is one event recorded twice.
        path = self.write_transcript(
            [
                user_line("continue", timestamp="2026-09-17T10:00:00.000Z"),
                assistant_line([text_block("ok")], timestamp="2026-09-17T10:00:01.000Z"),
                user_line("continue", timestamp="2026-09-17T10:00:02.000Z"),
            ]
        )
        out = self.run_extract(path)
        self.assertEqual(
            self.blocks_of(out),
            [("user", "continue"), ("assistant", "ok"), ("user", "continue")],
        )

    def test_a_sidechain_queued_command_is_excluded(self):
        path = self.write_transcript(
            [queued_command_line("SUBAGENT-ENQUEUE", isSidechain=True), user_line("Mine.")]
        )
        out = self.run_extract(path)
        self.assertNotIn("SUBAGENT-ENQUEUE", out)
        self.assertEqual(self.blocks_of(out), [("user", "Mine.")])

    def test_an_empty_or_non_string_prompt_yields_no_block(self):
        path = self.write_transcript(
            [
                queued_command_line(""),
                queued_command_line(None),
                {"type": "attachment", "attachment": {"type": "queued_command"}},
                user_line("survivor"),
            ]
        )
        self.assertEqual(self.blocks_of(self.run_extract(path)), [("user", "survivor")])


class SlashCommandEnvelopes(ExtractTestCase):
    """A slash-command invocation arrives as three sibling envelopes and nothing else. The name
    and the canned message are harness chrome; the args are what the user typed."""

    def test_command_chrome_is_stripped_and_typed_args_survive(self):
        path = self.write_transcript(
            [
                user_line(
                    "<command-name>/gitian-kb:handoff</command-name>\n"
                    "            <command-message>handoff</command-message>\n"
                    "            <command-args>focus on the hooks bundle</command-args>"
                )
            ]
        )
        out = self.run_extract(path)
        self.assertNotIn("command-name", out)
        self.assertNotIn("gitian-kb:handoff", out)
        self.assertNotIn("command-args", out)
        self.assertEqual(self.blocks_of(out), [("user", "focus on the hooks bundle")])

    def test_a_bare_slash_command_with_no_args_produces_no_block(self):
        path = self.write_transcript(
            [
                user_line(
                    "<command-name>/status</command-name>\n"
                    "            <command-message>status</command-message>\n"
                    "            <command-args></command-args>"
                ),
                user_line("Real question?"),
            ]
        )
        out = self.run_extract(path)
        self.assertEqual(self.blocks_of(out), [("user", "Real question?")])

    def test_a_system_reminder_inside_the_args_is_still_stripped(self):
        path = self.write_transcript(
            [
                user_line(
                    "<command-args>do it<system-reminder>INJECTED</system-reminder></command-args>"
                )
            ]
        )
        out = self.run_extract(path)
        self.assertNotIn("INJECTED", out)
        self.assertEqual(self.blocks_of(out), [("user", "do it")])


class LastFlag(ExtractTestCase):
    def test_last_keeps_only_the_most_recent_messages(self):
        path = self.write_transcript([user_line("m%d" % i) for i in range(6)])
        out = self.run_extract(path, "--last", "2")
        self.assertEqual(self.blocks_of(out), [("user", "m4"), ("user", "m5")])

    def test_last_counts_kept_messages_not_transcript_lines(self):
        # The dropped records (thinking-only assistant turn, tool_result user turn) must not
        # consume the --last budget -- otherwise `--last 2` can return nothing at all.
        path = self.write_transcript(
            [
                user_line("keep-1"),
                assistant_line([{"type": "thinking", "thinking": "noise"}]),
                user_blocks_line([{"type": "tool_result", "content": "noise"}]),
                assistant_line([text_block("keep-2")]),
            ]
        )
        out = self.run_extract(path, "--last", "2")
        self.assertEqual(
            self.blocks_of(out), [("user", "keep-1"), ("assistant", "keep-2")]
        )

    def test_unparsable_last_is_ignored_rather_than_fatal(self):
        path = self.write_transcript([user_line("only")])
        out = self.run_extract(path, "--last", "not-a-number")
        self.assertEqual(self.blocks_of(out), [("user", "only")])


class SinceFlag(ExtractTestCase):
    def test_since_drops_older_messages(self):
        path = self.write_transcript(
            [
                user_line("old", timestamp="2026-09-17T08:00:00.000Z"),
                user_line("new", timestamp="2026-09-17T12:00:00.000Z"),
            ]
        )
        out = self.run_extract(path, "--since", "2026-09-17T10:00:00Z")
        self.assertEqual(self.blocks_of(out), [("user", "new")])

    def test_since_accepts_a_bare_date(self):
        path = self.write_transcript(
            [
                user_line("yesterday", timestamp="2026-09-16T23:00:00.000Z"),
                user_line("today", timestamp="2026-09-17T01:00:00.000Z"),
            ]
        )
        out = self.run_extract(path, "--since", "2026-09-17")
        self.assertEqual(self.blocks_of(out), [("user", "today")])

    def test_unparsable_since_is_ignored_rather_than_fatal(self):
        path = self.write_transcript([user_line("kept")])
        out = self.run_extract(path, "--since", "whenever")
        self.assertEqual(self.blocks_of(out), [("user", "kept")])


class MaxCharsTruncation(ExtractTestCase):
    def test_truncation_keeps_the_newest_and_announces_the_omission(self):
        path = self.write_transcript([user_line("%d-%s" % (i, "x" * 200)) for i in range(10)])
        out = self.run_extract(path, "--max-chars", "700")

        self.assertTrue(
            out.startswith("[truncated: "), msg="no truncation banner: %r" % out[:120]
        )
        self.assertIn("earlier messages omitted]", out.split("\n")[0])
        self.assertLessEqual(len(out), 700)

        blocks = self.blocks_of(out)
        self.assertTrue(blocks)
        # The newest message survives; the oldest is gone.
        self.assertIn("9-", blocks[-1][1])
        self.assertNotIn("0-x", out)

        omitted = int(out.split("[truncated: ")[1].split(" ")[0])
        self.assertEqual(omitted + len(blocks), 10)

    def test_no_banner_when_everything_fits(self):
        path = self.write_transcript([user_line("small")])
        out = self.run_extract(path, "--max-chars", "5000")
        self.assertNotIn("[truncated:", out)

    def test_default_budget_truncates_a_very_large_transcript(self):
        # C4's default is 120,000 characters -- proven by behavior rather than by importing the
        # constant, so the CLI contract is what is pinned.
        path = self.write_transcript([user_line("%d-%s" % (i, "y" * 2000)) for i in range(100)])
        out = self.run_extract(path)
        self.assertTrue(out.startswith("[truncated: "))
        self.assertLessEqual(len(out), 120000)

    def test_a_single_oversized_message_is_still_emitted(self):
        # Documented behavior: the budget bounds HOW MANY messages are kept, never how large one
        # message may be -- returning an empty extract for a long final answer would be useless.
        path = self.write_transcript([user_line("z" * 5000)])
        out = self.run_extract(path, "--max-chars", "100")
        self.assertIn("z" * 5000, out)
        self.assertNotIn("[truncated:", out)

    def test_non_positive_max_chars_falls_back_to_the_default(self):
        path = self.write_transcript([user_line("small")])
        out = self.run_extract(path, "--max-chars", "0")
        self.assertEqual(self.blocks_of(out), [("user", "small")])


class FailOpen(ExtractTestCase):
    def test_missing_file_is_silent_and_exits_zero(self):
        self.assertEqual(self.run_extract(os.path.join(self.tmpdir, "nope.jsonl")), "")

    def test_no_arguments_is_silent_and_exits_zero(self):
        self.assertEqual(self.run_extract(), "")

    def test_malformed_lines_are_skipped_and_the_rest_survives(self):
        path = self.write_transcript(
            [
                "{not valid json ][ at all",
                "",
                "[1, 2, 3]",
                json.dumps({"type": "user", "message": "not-an-object"}),
                user_line("survivor"),
            ]
        )
        self.assertEqual(self.blocks_of(self.run_extract(path)), [("user", "survivor")])

    def test_a_directory_instead_of_a_file_is_silent(self):
        self.assertEqual(self.run_extract(self.tmpdir), "")

    def test_unknown_flags_are_ignored(self):
        path = self.write_transcript([user_line("still here")])
        out = self.run_extract(path, "--wat", "--last")
        self.assertEqual(self.blocks_of(out), [("user", "still here")])


if __name__ == "__main__":
    unittest.main()
