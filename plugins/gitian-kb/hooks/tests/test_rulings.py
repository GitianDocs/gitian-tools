#!/usr/bin/env python3
"""Unit tests for rulings.py, the heuristic behind prompt_checkpoint.py that decides whether an
owner's prompt states a standing rule (and so becomes a `ruling` event in the activity ledger).

The contract's listed hits and misses are one test case each (MustHit / MustMiss). The rest pin what
is never the owner's own words (slash commands, injected tag blocks, code, quotes), the sentence
skips (questions, first person, code and logs), the ALL-CAPS cue, the excerpt shape and the
fail-open edges. Precision beats recall here, so every "is ignored" test carries a CONTROL that
does hit: a detector that never fires cannot pass them.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_rulings.py
"""

import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOKS_DIR))

import rulings  # noqa: E402  (the module under test lives one directory up)

detect = rulings.detect

# The contract's "Must hit": each is a single sentence, so the excerpt is the prompt itself.
MUST_HIT = [
    "DO NOT USE FABLE TO WRITE CODE USE SONNET 5",
    "releases are never posted before prod is fully deployed",
    "From now on, heavy CI runs on the build box.",
    "Never merge with --auto on gitian.",
    "X cannot happen until Y is green",
    "you must never run remote work as the owner",
    "Going forward, each org gets its own runner user",
]

# A fenced log with a shouted NEVER in it, and a pasted block holding a durable-scope sentence.
FENCED_LOG = "Here is the build log:\n```\nNEVER reached the deploy step\n```\nThat is all."
PASTED_BLOCK = '<pasted_content name="notes.txt">\nFrom now on, always squash.\n</pasted_content>'

# The contract's "Must miss".
MUST_MISS = [
    "never mind",
    "I never got the email",
    "it always fails on CI",
    "I can't do it until tomorrow",
    "Should we always rebase?",
    "ERROR: build failed",
    "The PR CI is OK",
    "ok",
    "Hi",
    FENCED_LOG,
    PASTED_BLOCK,
]

FIRST_PERSON_OPENERS = ("I", "i", "I'm", "Im", "I'll", "I've", "I'd", "I\u2019ll")

# Six words, none of them a cue: filler that makes a sentence long without hitting.
FILLER_WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:44]


class MustHit(unittest.TestCase):
    """One generated test per prompt of MUST_HIT: the excerpt is the sentence, whole."""


class MustMiss(unittest.TestCase):
    """One generated test per prompt of MUST_MISS."""


def _register(cls, prefix, prompts, make):
    for number, prompt in enumerate(prompts, 1):
        setattr(cls, "test_%s_%02d_%s" % (prefix, number, _slug(prompt)), make(prompt))


def _hit_test(prompt):
    def test(self):
        self.assertEqual(detect(prompt), prompt)

    return test


def _miss_test(prompt):
    def test(self):
        self.assertIsNone(detect(prompt))

    return test


_register(MustHit, "hit", MUST_HIT, _hit_test)
_register(MustMiss, "miss", MUST_MISS, _miss_test)


class Controls(unittest.TestCase):
    """Controls for the two MUST_MISS prompts ignored for where they sit: unwrapped, they hit."""

    def test_the_fenced_log_line_hits_when_it_is_not_fenced(self):
        self.assertEqual(detect("NEVER reached the deploy step"), "NEVER reached the deploy step")

    def test_the_pasted_sentence_hits_when_it_is_not_in_a_block(self):
        self.assertEqual(detect("From now on, always squash."), "From now on, always squash.")


class ShoutedRunStart(unittest.TestCase):
    """A shouted run counts only when it STARTS with a trigger word: in a run of the owner's real
    prompts, all-caps venting with NOT or STOP mid-run was the largest group of false hits."""

    def test_trigger_mid_run_is_venting_not_a_rule(self):
        self.assertIsNone(detect("WHY IS THIS NOT WORKING AGAIN"))
        self.assertIsNone(detect("THE BUILD DID NOT STOP"))

    def test_trigger_opening_the_run_still_hits(self):
        self.assertIsNotNone(detect("DO NOT USE FABLE TO WRITE CODE USE SONNET 5"))
        self.assertIsNotNone(detect("ONLY MERGE AFTER CI IS GREEN"))


class Excerpt(unittest.TestCase):
    def test_whitespace_is_collapsed(self):
        self.assertEqual(
            detect("Never \t merge   with\u00a0--auto \u3000 on gitian."),
            "Never merge with --auto on gitian.",
        )

    def test_exactly_160_chars_is_whole_and_161_is_cut(self):
        whole = "Never merge " + "x" * 148
        self.assertEqual(len(whole), 160)
        self.assertEqual(detect(whole), whole)
        cut = detect(whole + "x")
        self.assertLessEqual(len(cut), 160)
        self.assertTrue(cut.endswith("..."))

    def test_a_long_sentence_is_cut_on_a_word_boundary_with_an_ellipsis(self):
        sentence = "Never " + " ".join(FILLER_WORDS * 12) + "."
        out = detect(sentence)
        self.assertLessEqual(len(out), 160)
        self.assertGreaterEqual(len(out), 145)  # cut as late as a word boundary allows
        self.assertTrue(out.endswith("..."))
        head = out[:-3]
        self.assertTrue(sentence.startswith(head))
        self.assertEqual(sentence[len(head)], " ")  # whole words only, never "charl..."

    def test_the_cut_leaves_no_dangling_separator(self):
        out = detect("Never " + "w, " * 100)
        self.assertTrue(out.endswith("w..."), out[-12:])

    def test_a_sentence_with_nothing_to_cut_on_still_obeys_the_cap(self):
        out = detect("Never " + "y" * 400)
        self.assertLessEqual(len(out), 160)
        self.assertTrue(out.endswith("..."))

    def test_a_cue_past_the_head_pulls_the_window_to_it(self):
        sentence = " ".join(FILLER_WORDS * 15) + ", always run the linter before pushing."
        out = detect(sentence)
        self.assertLessEqual(len(out), 160)
        self.assertTrue(out.startswith("..."))
        self.assertIn("always run the linter", out)
        self.assertGreaterEqual(out.index(", always") - 3, 40)  # 40+ chars of context first
        window = out[3:]
        self.assertTrue(sentence.endswith(window))
        self.assertEqual(sentence[-len(window) - 1], " ")  # the window starts on a word

    def test_a_sentence_that_fits_is_never_windowed_whatever_the_cue_position(self):
        sentence = " ".join(FILLER_WORDS * 3) + ", always squash."
        self.assertLess(len(sentence), 160)
        self.assertGreater(sentence.index("always"), 80)
        self.assertEqual(detect(sentence), sentence)

    def test_a_cue_inside_the_head_keeps_the_head(self):
        out = detect("Alpha bravo, never merge " + " ".join(FILLER_WORDS * 12) + ".")
        self.assertTrue(out.startswith("Alpha bravo, never merge"))
        self.assertTrue(out.endswith("..."))
        self.assertLessEqual(len(out), 160)


class SlashCommands(unittest.TestCase):
    def test_a_prompt_starting_with_a_slash_is_skipped(self):
        for prompt in (
            "/handoff never merge on Fridays",
            "  /handoff never merge on Fridays",
            "\n\n/gitian-kb:status From now on, always squash.",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(detect(prompt))

    def test_a_slash_further_in_does_not_skip(self):
        self.assertEqual(detect("See /handoff. Never merge on Fridays."), "Never merge on Fridays.")


class TagBlocks(unittest.TestCase):
    RULE = "From now on, always squash."
    BLOCKS = {
        "pasted_content": '<pasted_content name="notes.txt">\n%s\n</pasted_content>',
        "cross-session-message": (
            '<cross-session-message from="session-2" sent="2026-10-24T08:00:00Z">\n%s\n'
            "</cross-session-message>"
        ),
        "system-reminder": "<system-reminder>\n%s\n</system-reminder>",
        "task-notification": "<task-notification>\n%s\n</task-notification>",
    }

    def assert_block_is_dropped(self, name):
        block = self.BLOCKS[name] % self.RULE
        self.assertIsNone(detect(block))
        # Only the block goes: the owner's own sentences before and after it are still scanned.
        self.assertEqual(detect("Never merge on Fridays.\n" + block), "Never merge on Fridays.")
        self.assertEqual(detect(block + "\nAlways lint."), "Always lint.")
        self.assertEqual(detect(self.RULE), self.RULE)  # control: unwrapped, the rule hits

    def test_pasted_content_block_is_dropped(self):
        self.assert_block_is_dropped("pasted_content")

    def test_cross_session_message_block_is_dropped(self):
        self.assert_block_is_dropped("cross-session-message")

    def test_system_reminder_block_is_dropped(self):
        self.assert_block_is_dropped("system-reminder")

    def test_task_notification_block_is_dropped(self):
        self.assert_block_is_dropped("task-notification")

    def test_a_reminder_full_of_rules_is_not_the_owner(self):
        reminder = (
            "<system-reminder>\nAlways follow the repo rules. Never skip tests. "
            "You must always run lint before you commit.\n</system-reminder>"
        )
        self.assertIsNone(detect(reminder))
        self.assertIsNone(detect("ok thanks\n" + reminder))

    def test_same_name_blocks_nest_and_go_whole(self):
        nested = (
            "<system-reminder>outer <system-reminder>inner</system-reminder> Never leaks. "
            "</system-reminder>"
        )
        self.assertIsNone(detect(nested))
        self.assertEqual(detect(nested + "\nAlways lint."), "Always lint.")

    def test_a_dropped_block_is_a_sentence_break_not_a_join(self):
        self.assertEqual(
            detect("Please <pasted_content>x</pasted_content> never merge on Fridays."),
            "never merge on Fridays.",
        )

    def test_a_tag_name_matches_case_insensitively(self):
        self.assertIsNone(detect("<System-Reminder>\nNever merge\n</system-reminder>"))

    def test_a_block_goes_whole_whatever_backticks_it_carries(self):
        # Stripped first, a block holding a stray backtick cannot pair it with one outside and
        # leave its own closing tag behind (which would leak the block's sentences).
        prompt = (
            "<pasted_content>\nuse ` char\nFrom now on, always squash.\n</pasted_content>\n"
            "thanks. And `y` is fine."
        )
        self.assertIsNone(detect(prompt))

    def test_a_fence_opened_inside_a_block_does_not_swallow_the_prompt(self):
        prompt = "<system-reminder>\n```\nfoo\n</system-reminder>\nNever merge on Fridays."
        self.assertEqual(detect(prompt), "Never merge on Fridays.")

    def test_a_closer_pairs_with_the_nearest_opener_so_a_stray_opener_drops_nothing(self):
        prompt = "Use <x> as a marker. Never merge on Fridays.\n<x>junk</x>"
        self.assertEqual(detect(prompt), "Never merge on Fridays.")

    def test_a_self_closing_tag_is_not_an_opener(self):
        self.assertIsNone(detect("<x>\nNever merge on Fridays.\n<x/> more\n</x>"))

    def test_an_unclosed_tag_drops_nothing(self):
        # Generics and void elements are not blocks; dropping to the end would hide the rest.
        for prompt in (
            "Promise<void> resolves first. Never merge on Fridays.",
            "Use <br> to break lines. Never merge on Fridays.",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(detect(prompt), "Never merge on Fridays.")


class CodeIsNotProse(unittest.TestCase):
    def test_a_fenced_block_is_ignored(self):
        for opener, closer in (
            ("```", "```"),
            ("```text", "```"),
            ("~~~", "~~~"),
            ("````", "````"),
            ("   ```", "   ```"),
        ):
            with self.subTest(opener=opener):
                prompt = "%s\nNEVER reached the deploy step\n%s" % (opener, closer)
                self.assertIsNone(detect(prompt))

    def test_text_around_a_fenced_block_is_still_scanned(self):
        self.assertEqual(
            detect("Never merge on Fridays.\n```\nNEVER\n```\nAlways lint."),
            "Never merge on Fridays.",
        )
        self.assertEqual(detect("```\nNEVER\n```\nAlways lint."), "Always lint.")

    def test_a_longer_fence_holds_shorter_fences_inside_it(self):
        self.assertEqual(detect("````md\n```\nNEVER\n```\n````\nAlways lint."), "Always lint.")

    def test_a_fence_is_closed_only_by_its_own_character_and_nothing_after_it(self):
        # a tilde line does not close a backtick fence, and neither does text after the backticks
        self.assertIsNone(detect("```\nNEVER\n~~~\nAlways lint."))
        self.assertIsNone(detect("```\nNEVER\n``` still code\nAlways lint."))
        self.assertEqual(detect("~~~\nNEVER\n~~~  \nAlways lint."), "Always lint.")

    def test_a_fenced_block_separates_paragraphs(self):
        prompt = "stray ` before\n```\ncode\n```\nNever merge on Fridays. stray ` after."
        self.assertEqual(detect(prompt), "Never merge on Fridays.")

    def test_an_unterminated_fence_runs_to_the_end_of_the_prompt(self):
        self.assertIsNone(detect("```\nNEVER reached it\nAlways lint."))

    def test_inline_code_is_ignored(self):
        for code, plain in (
            ("The label says `from now on`.", "The label says from now on."),
            ("Set the flag to `NEVER`.", "Set the flag to NEVER."),
            ("Type ``from now on`x`` to begin.", "Type from now on`x to begin."),
        ):
            with self.subTest(code=code):
                self.assertIsNone(detect(code))
                self.assertIsNotNone(detect(plain))  # control: the same words as prose hit

    def test_dropping_a_span_never_fuses_the_words_around_it(self):
        self.assertEqual(detect("Always`x`squash"), "Always squash")

    def test_a_code_span_is_dropped_not_replaced(self):
        self.assertEqual(detect("Never run `rm -rf` without asking."), "Never run without asking.")

    def test_a_line_that_opens_and_closes_a_triple_backtick_is_not_a_fence(self):
        self.assertEqual(detect("```NEVER``` is the marker. Always squash."), "Always squash.")

    def test_a_span_never_crosses_a_blank_line(self):
        prompt = "A stray ` here.\n\nNever merge on Fridays.\n\nAnother stray ` there."
        self.assertEqual(detect(prompt), "Never merge on Fridays.")


class QuoteLines(unittest.TestCase):
    def test_quoted_lines_are_ignored(self):
        for prompt in (
            "> From now on, always squash.",
            "  > never merge before CI is green",
            ">> NEVER",
            ">quoted without a space: always squash",
        ):
            with self.subTest(prompt=prompt):
                self.assertIsNone(detect(prompt))
                self.assertIsNotNone(detect(prompt.lstrip("> ")))  # control: unquoted, it hits

    def test_only_the_quoted_lines_go(self):
        self.assertEqual(
            detect("> From now on, always squash.\nNever merge on Fridays."),
            "Never merge on Fridays.",
        )
        self.assertEqual(detect("Never merge on Fridays.\n> NEVER"), "Never merge on Fridays.")


class ScanWindow(unittest.TestCase):
    PROSE = "lorem ipsum dolor sit amet. "  # 28 chars, no cue

    def test_a_rule_past_the_first_4000_chars_is_not_scanned(self):
        self.assertIsNone(detect(self.PROSE * 150 + "Never merge on Fridays."))  # starts at 4200
        self.assertEqual(
            detect(self.PROSE * 140 + "Never merge on Fridays."), "Never merge on Fridays."
        )  # ends at 3943

    def test_the_budget_counts_what_is_left_after_stripping(self):
        fenced = "```\n" + "lorem ipsum dolor sit amet\n" * 400 + "```\n"
        self.assertGreater(len(fenced), 4000)
        self.assertEqual(detect(fenced + "Never merge on Fridays."), "Never merge on Fridays.")


class Skips(unittest.TestCase):
    def test_a_question_is_skipped(self):
        for prompt in MUST_HIT:
            with self.subTest(prompt=prompt):
                self.assertIsNone(detect(prompt.rstrip(".") + "?"))

    def test_a_question_with_a_closing_mark_is_still_a_question(self):
        self.assertIsNone(detect("Should I never merge before CI?)"))
        self.assertIsNone(detect('He asked "never merge before CI?"'))

    def test_only_the_question_sentence_is_skipped(self):
        self.assertEqual(
            detect("Should we rebase? Never merge on Fridays."), "Never merge on Fridays."
        )

    def test_first_person_openers_skip_patterns_2_to_4(self):
        for template in (
            "%s said: always squash",  # 2: imperative at a clause start
            "%s should always rebase",  # 3: modal
            "%s never merge before CI is green",  # 4: conditional prohibition
        ):
            for first in FIRST_PERSON_OPENERS:
                with self.subTest(template=template, first=first):
                    self.assertIsNone(detect(template % first))
            with self.subTest(template=template, first="We"):
                self.assertIsNotNone(detect(template % "We"))  # control: not first person

    def test_first_person_does_not_skip_the_durable_scope_cue(self):
        prompt = "I want this from now on: always squash"
        self.assertEqual(detect(prompt), prompt)
        for first in FIRST_PERSON_OPENERS:
            with self.subTest(first=first):
                self.assertIsNotNone(detect("%s want this going forward" % first))

    # (text with a code or log marker, the same words without it). The first must be ignored; the
    # second is the control that hits.
    CODE_AND_LOGS = [
        ("NEVER use {braces}", "NEVER use braces"),
        ("NEVER close } early", "NEVER close early"),
        ("NEVER use a => b", "NEVER use a then b"),
        ("NEVER write a == b", "NEVER write a then b"),
        ("NEVER call std::vector", "NEVER call vector"),
        ("$ DO NOT RUN THIS", "DO NOT RUN THIS"),
        ("ERROR: DO NOT USE THIS", "WARNING: DO NOT USE THIS"),
        ("[ERROR] DO NOT USE THIS", "[NOTE] DO NOT USE THIS"),
        ("warn: you should never see this", "note: you should never see this"),
        ("info: cannot open it until later", "note: cannot open it until later"),
        ("2026-10-24T08:00:01Z NEVER reached the deploy step", "NEVER reached the deploy step"),
        ("[08:00:01] MUST fail", "MUST fail"),
    ]

    def test_code_and_log_lines_are_ignored(self):
        for text, control in self.CODE_AND_LOGS:
            with self.subTest(text=text):
                self.assertIsNone(detect(text))
                self.assertIsNotNone(detect(control))

    def test_a_warning_label_is_emphasis_not_a_log_level(self):
        self.assertEqual(detect("WARNING: never force push."), "WARNING: never force push.")


class AllCaps(unittest.TestCase):
    def test_a_lone_shouted_verdict_word_is_a_cue(self):
        for word in ("NEVER", "ALWAYS", "MUST", "MUSTN'T", "CANNOT"):
            with self.subTest(word=word):
                text = "merge on fridays %s" % word
                self.assertEqual(detect(text), text)

    def test_shouting_is_case_sensitive(self):
        for text in (
            "merge on fridays never",
            "merge on fridays always",
            "merge on fridays Must",
            "Do Not Use Fable To Write Code",
            "do not use fable to write code use sonnet 5",
        ):
            with self.subTest(text=text):
                self.assertIsNone(detect(text))

    def test_shouted_never_mind_is_not_a_rule(self):
        for text in ("NEVER MIND", "NEVER mind", "Never MIND"):
            with self.subTest(text=text):
                self.assertIsNone(detect(text))

    def test_the_run_triggers_are_the_contracts(self):
        self.assertEqual(
            rulings.SHOUT_RUN_TRIGGERS,
            {"NOT", "DON'T", "DONT", "NO", "ONLY", "STOP", "DO", "CAN'T"},
        )

    def test_a_shouted_run_with_a_trigger_is_a_cue(self):
        for trigger in sorted(rulings.SHOUT_RUN_TRIGGERS):
            with self.subTest(trigger=trigger):
                self.assertIsNotNone(detect("%s MERGE" % trigger))

    def test_a_shouted_run_without_a_trigger_is_not(self):
        for text in ("MERGE EARLY", "BUILD FAILED", "THE BUILD FAILED AGAIN"):
            with self.subTest(text=text):
                self.assertIsNone(detect(text))

    def test_a_lone_trigger_word_is_not_a_run(self):
        for trigger in sorted(rulings.SHOUT_RUN_TRIGGERS):
            with self.subTest(trigger=trigger):
                self.assertIsNone(detect(trigger))
                self.assertIsNone(detect("merge on fridays %s" % trigger))

    def test_a_run_is_whitespace_separated_shouted_words_of_two_or_more_letters(self):
        for text in (
            "NO, MERGE",  # punctuation ends a run
            "NO 5 MERGE",  # so does a digit
            "NO merge",  # and a lowercase word
            "NO A",  # one letter is not a shouted word
            "NO I",
        ):
            with self.subTest(text=text):
                self.assertIsNone(detect(text))

    def test_identifiers_are_not_shouting(self):
        for text in ("NO MAX_RETRIES", "NO SONNET5"):
            with self.subTest(text=text):
                self.assertIsNone(detect(text))

    def test_quote_marks_around_shouted_words_do_not_hide_them(self):
        self.assertIsNotNone(detect("'DO NOT' merge on fridays"))
        self.assertIsNotNone(detect("merge on fridays 'NEVER'"))

    def test_the_acronym_list_is_the_contracts(self):
        self.assertEqual(
            rulings.ACRONYMS,
            set(
                "PR CI CD API URL KB ID OK SSH VPN DNS AWS GCP UI UX DB SQL JSON YAML HTTP HTTPS"
                " MCP LLM JWT PAT TTY GPU CPU RAM APK USB TODO FYI ASAP".split()
            ),
        )

    def test_acronyms_never_count_toward_a_run(self):
        for acronym in sorted(rulings.ACRONYMS):
            with self.subTest(acronym=acronym):
                self.assertIsNone(detect("NO %s" % acronym))
                self.assertIsNone(detect("%s STOP" % acronym))
        self.assertIsNone(detect("PR CI API"))
        self.assertIsNotNone(detect("NO MERGE"))  # control: the same shape with a real word hits

    def test_an_acronym_after_a_run_does_not_undo_it(self):
        self.assertEqual(detect("DO NOT MERGE THE PR"), "DO NOT MERGE THE PR")

    def test_curly_apostrophes_are_folded_to_ascii(self):
        for text in (
            "DON\u2019T MERGE",
            "merge on fridays MUSTN\u2019T",
            "Don\u2019t merge until CI is green",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(detect(text))


class Patterns(unittest.TestCase):
    """Every alternative of the four prose cues, so a typo in a regex cannot hide."""

    def test_durable_scope_phrases(self):
        for phrase in (
            "from now on",
            "going forward",
            "from here on",
            "from here on out",
            "henceforth",
            "in the future",
            "in future",
            "for future reference",
            "as a rule",
            "as a general rule",
            "standing order",
            "standing rule",
            "standing instruction",
            "new rule",
            "the rule is",
            "every time you",
            "each time you",
            "any time you",
            "everytime you",
            "anytime you",
            "whenever you",
        ):
            with self.subTest(phrase=phrase):
                text = "Ok so %s we use sonnet" % phrase
                self.assertEqual(detect(text), text)
                self.assertIsNotNone(detect(text.upper()))  # case-insensitive

    def test_near_misses_of_the_durable_scope_phrases(self):
        for text in (
            "We should go forward with it",
            "The future of this is unclear",
            "In the near future we may know",
            "A rule of thumb",
            "From now we wait",
        ):
            with self.subTest(text=text):
                self.assertIsNone(detect(text))

    def test_imperative_always_or_never_at_a_clause_start(self):
        for text in (
            "Always squash",
            "never merge on Fridays",
            "Ok, always squash",
            "Ok; never squash",
            "Note: always squash",
            "and always squash",
            "but never squash",
            "so always squash",
            "please always squash",
            "just never squash",
            "also always squash",
            "then always squash",
            "Don't ever merge on Fridays",
            "Do not ever merge on Fridays",
            "Please do not ever merge",
        ):
            with self.subTest(text=text):
                self.assertEqual(detect(text), text)

    def test_always_or_never_in_the_middle_of_a_clause_is_a_report(self):
        for text in (
            "the build always fails",
            "it never works",
            "my laptop never boots",
            "we are never done",
            "you don't ever listen",
        ):
            with self.subTest(text=text):
                self.assertIsNone(detect(text))

    def test_never_mind_is_never_a_prohibition(self):
        for text in (
            "Never mind that",
            "nevermind",
            "Never mind, I'll do it before lunch.",
            "Never mind, check it before you merge.",
        ):
            with self.subTest(text=text):
                self.assertIsNone(detect(text))

    def test_modal_always_or_never(self):
        for modal in ("must", "should", "shall", "has to", "have to", "need to", "needs to"):
            for adverb in ("always", "never"):
                with self.subTest(modal=modal, adverb=adverb):
                    text = "The agent %s %s rebase" % (modal, adverb)
                    self.assertEqual(detect(text), text)

    def test_a_modal_and_its_adverb_must_be_adjacent(self):
        self.assertIsNone(detect("Should we always rebase"))
        self.assertIsNone(detect("The agent must be fast"))

    def test_conditional_prohibition(self):
        for prohibition in (
            "never",
            "cannot",
            "can't",
            "cant",
            "can not",
            "must not",
            "mustn't",
            "may not",
            "should not",
            "shouldn't",
            "do not",
            "don't",
        ):
            for connector in ("until", "unless", "before", "without"):
                with self.subTest(prohibition=prohibition, connector=connector):
                    text = "We %s merge %s review" % (prohibition, connector)
                    self.assertEqual(detect(text), text)

    def test_a_prohibition_needs_its_condition_after_it_in_the_same_sentence(self):
        self.assertIsNone(detect("We do not merge"))
        self.assertIsNone(detect("Before review we cannot merge"))
        self.assertIsNone(detect("We never merge. Until review it waits."))

    def test_the_condition_must_be_within_80_characters(self):
        self.assertIsNotNone(detect("We never merge %s until review" % ("x" * 60)))
        self.assertIsNone(detect("We never merge %s until review" % ("x" * 100)))


class Sentences(unittest.TestCase):
    def test_the_first_matching_sentence_alone_is_returned(self):
        prompt = (
            "Thanks for the update. Going forward, run the linter first. Never merge on Fridays."
        )
        self.assertEqual(detect(prompt), "Going forward, run the linter first.")

    def test_each_line_is_its_own_sentence(self):
        self.assertEqual(detect("Status looks fine\nAlways lint\nok"), "Always lint")
        self.assertEqual(detect("Status looks fine\r\nAlways lint\r\nok"), "Always lint")

    def test_every_line_end_ends_a_sentence(self):
        for end in ("\r", "\r\n", "\u2028", "\u2029"):
            with self.subTest(end=repr(end)):
                prompt = "Status looks fine%sAlways lint%sok" % (end, end)
                self.assertEqual(detect(prompt), "Always lint")

    def test_list_markers_are_not_part_of_the_excerpt(self):
        for prompt in (
            "- Never merge on Fridays",
            "* Never merge on Fridays",
            "1. Never merge on Fridays",
            "2) Never merge on Fridays",
            '"Never merge on Fridays',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(detect(prompt), "Never merge on Fridays")

    def test_an_ellipsis_does_not_end_a_sentence(self):
        self.assertEqual(detect("ok... from now on, use sonnet"), "ok... from now on, use sonnet")

    def test_exclamations_end_sentences(self):
        self.assertEqual(detect("Great! Never merge on Fridays!"), "Never merge on Fridays!")


class FailOpen(unittest.TestCase):
    def test_non_strings_are_none(self):
        for value in (
            None,
            42,
            4.2,
            True,
            [],
            ["Never merge."],
            {},
            {"prompt": "Never merge."},
            b"Never merge.",
            bytearray(b"Never merge."),
            object(),
        ):
            with self.subTest(value=repr(value)):
                self.assertIsNone(detect(value))

    def test_empty_prompts_are_none(self):
        for prompt in ("", " ", "\n\n", "\t \n"):
            with self.subTest(prompt=repr(prompt)):
                self.assertIsNone(detect(prompt))

    def test_an_internal_error_is_no_ruling(self):
        with mock.patch.object(rulings, "_clean", side_effect=RuntimeError("boom")):
            self.assertIsNone(detect("Never merge on Fridays."))
        with mock.patch.object(rulings, "_judge", side_effect=RuntimeError("boom")):
            self.assertIsNone(detect("Never merge on Fridays."))

    def test_odd_text_does_not_raise(self):
        for prompt in (
            "\x00Never\x00",
            "\ud800 never merge",
            "\u202eNever merge",
            "<<<>>>",
            "`" * 5,
            "~" * 5,
            ")(" * 100,
            "????",
            "...",
            "<a",
            "</a>",
        ):
            with self.subTest(prompt=repr(prompt)):
                result = detect(prompt)
                self.assertTrue(result is None or isinstance(result, str))

    def test_a_huge_adversarial_prompt_is_handled_in_bounded_time(self):
        prompt = (
            "<br>" * 50000 + "`a ``b ```c " * 20000 + "<a>" * 20000 + "\n" * 100000
            + "> quote\n" * 20000 + "Never merge on Fridays."
        )
        started = time.monotonic()
        result = detect(prompt)
        self.assertLess(time.monotonic() - started, 10)
        self.assertTrue(result is None or isinstance(result, str))

    def test_the_contract_limits(self):
        self.assertEqual(rulings.EXCERPT_MAX, 160)
        self.assertEqual(rulings.SCAN_CHARS, 4000)


if __name__ == "__main__":
    unittest.main()
