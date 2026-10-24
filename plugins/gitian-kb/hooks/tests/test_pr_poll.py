#!/usr/bin/env python3
"""Unit tests for pr_poll.py, the one-round-trip GitHub poll behind the Stop checkpoint's merge
detection.

The pure functions (entry_key, build_query, parse_results) get direct tests. run_query is driven
through an injected `subprocess.run`-shaped runner, so every way gh can go wrong is a canned answer,
and poll() is checked end to end the same way. One class then runs the REAL subprocess path
against a fake `gh` script placed first on PATH, so the argv, the environment, stdin, the
exit-code handling and the timeout are proven on a real child process. No test needs a network, a
login or GitHub.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_pr_poll.py
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOKS_DIR))

import pr_poll  # noqa: E402  (the module under test lives one directory up)

API = "acme/api"
WEB = "acme/web"


def watch(repo, number=None, branch=None, why="opened"):
    """A watch entry shaped the way ledger.add_watch stores it."""
    return {
        "repo": repo,
        "number": number,
        "branch": branch,
        "why": why,
        "addedAt": "2026-10-24T08:00:00Z",
        "polledAt": None,
    }


def node(number, state="OPEN", title=None, merged_at=None):
    """One pull request as GitHub's GraphQL answers it."""
    return {
        "number": number,
        "state": state,
        "title": "PR %d" % number if title is None else title,
        "mergedAt": merged_at,
    }


# Two repositories, a number and a branch in the first, interleaved: the shape the Stop checkpoint
# really polls. The document, keymap, answer and parsed result below belong together.
TWO_REPOS = [
    watch(API, number=650, why="merge"),
    watch(API, branch="feat/x", why="branch"),
    watch(WEB, number=7),
    watch(API, number=12),
]

TWO_REPOS_QUERY = "\n".join(
    [
        "query {",
        '  r0: repository(owner: "acme", name: "api") {',
        "    p650: pullRequest(number: 650) { number state title mergedAt }",
        '    b0: pullRequests(headRefName: "feat/x", last: 1, states: [OPEN, MERGED, CLOSED]) '
        "{ nodes { number state title mergedAt } }",
        "    p12: pullRequest(number: 12) { number state title mergedAt }",
        "  }",
        '  r1: repository(owner: "acme", name: "web") {',
        "    p7: pullRequest(number: 7) { number state title mergedAt }",
        "  }",
        "}",
    ]
)

TWO_REPOS_KEYMAP = {
    "r0": {"p650": "acme/api#650", "b0": "acme/api@feat/x", "p12": "acme/api#12"},
    "r1": {"p7": "acme/web#7"},
}

TWO_REPOS_DATA = {
    "r0": {
        "p650": node(650, "MERGED", "Ship the thing", "2026-10-24T07:00:00Z"),
        "b0": {"nodes": [node(651, "OPEN", "Next step")]},
        "p12": node(12, "CLOSED", "Abandoned"),
    },
    "r1": {"p7": node(7, "OPEN", "Still going")},
}

TWO_REPOS_RESULTS = {
    "acme/api#650": {
        "state": "MERGED",
        "number": 650,
        "title": "Ship the thing",
        "mergedAt": "2026-10-24T07:00:00Z",
    },
    "acme/api@feat/x": {"state": "OPEN", "number": 651, "title": "Next step", "mergedAt": None},
    "acme/api#12": {"state": "CLOSED", "number": 12, "title": "Abandoned", "mergedAt": None},
    "acme/web#7": {"state": "OPEN", "number": 7, "title": "Still going", "mergedAt": None},
}


def keys_of(keymap):
    return {key for fields in keymap.values() for key in fields.values()}


class FakeRunner:
    """A `subprocess.run`-shaped callable: records every call, then answers with a canned
    CompletedProcess or raises whatever it was told to."""

    def __init__(self, stdout="", returncode=0, stderr="", raises=None):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr
        self.raises = raises
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def answer(data, errors=None):
    """The JSON text `gh api graphql` prints for a response."""
    body = {"data": data}
    if errors is not None:
        body["errors"] = errors
    return json.dumps(body)


class EntryKey(unittest.TestCase):
    def test_a_number_keys_with_a_hash_and_a_branch_with_an_at_sign(self):
        self.assertEqual(pr_poll.entry_key(watch(API, number=650)), "acme/api#650")
        self.assertEqual(pr_poll.entry_key(watch(API, branch="feat/x")), "acme/api@feat/x")

    def test_a_bare_minimal_entry_is_enough(self):
        self.assertEqual(pr_poll.entry_key({"repo": API, "number": 3}), "acme/api#3")
        self.assertEqual(pr_poll.entry_key({"repo": API, "branch": "main"}), "acme/api@main")

    def test_a_number_beats_a_branch_on_the_same_entry(self):
        self.assertEqual(
            pr_poll.entry_key(watch(API, number=650, branch="feat/x")), "acme/api#650"
        )

    def test_the_repo_is_kept_exactly_as_written(self):
        # GitHub folds case, but the key is a lookup into the watch: it must not rewrite it.
        self.assertEqual(pr_poll.entry_key(watch("Acme/API", number=1)), "Acme/API#1")

    def test_an_entry_that_cannot_be_polled_has_no_key(self):
        for entry in (
            None,
            "acme/api#650",
            650,
            [],
            {},
            {"repo": API},
            watch(API),
            watch(API, branch=""),
            watch(API, branch=7),
            watch("nonsense", number=1),
            watch("a/b/c", number=1),
            watch(None, number=1),
            watch(API, number="1) { x"),
            watch(API, number=-5),
            watch(API, number=True),
        ):
            with self.subTest(entry=entry):
                self.assertIsNone(pr_poll.entry_key(entry))

    def test_it_agrees_with_build_query_about_which_entries_exist(self):
        entries = TWO_REPOS + [
            watch(API, number="1) { x"),
            watch("a/b/c", number=1),
            watch(API, branch="x\n"),
            watch(WEB, branch="main"),
        ]
        _, keymap = pr_poll.build_query(entries)
        expected = {key for key in (pr_poll.entry_key(e) for e in entries) if key}
        self.assertEqual(keys_of(keymap), expected)
        self.assertEqual(len(expected), 5)


class BuildQueryShape(unittest.TestCase):
    def test_two_repos_with_numbers_and_branches(self):
        query, keymap = pr_poll.build_query(TWO_REPOS)
        self.assertEqual(query, TWO_REPOS_QUERY)
        self.assertEqual(keymap, TWO_REPOS_KEYMAP)

    def test_the_field_forms_are_spelled_as_documented(self):
        query, _ = pr_poll.build_query(TWO_REPOS)
        self.assertIn('r0: repository(owner: "acme", name: "api") {', query)
        self.assertIn('r1: repository(owner: "acme", name: "web") {', query)
        self.assertIn("p650: pullRequest(number: 650) { number state title mergedAt }", query)
        self.assertIn(
            'b0: pullRequests(headRefName: "feat/x", last: 1, states: [OPEN, MERGED, CLOSED]) '
            "{ nodes { number state title mergedAt } }",
            query,
        )
        self.assertEqual(query.count("{"), query.count("}"))

    def test_the_same_watch_always_builds_the_same_document(self):
        first = pr_poll.build_query(TWO_REPOS)
        second = pr_poll.build_query(json.loads(json.dumps(TWO_REPOS)))
        self.assertEqual(first, second)

    def test_a_number_keeps_its_alias_wherever_it_sits(self):
        _, keymap = pr_poll.build_query([watch(API, number=12), watch(API, number=650)])
        self.assertEqual(keymap, {"r0": {"p12": "acme/api#12", "p650": "acme/api#650"}})
        _, keymap = pr_poll.build_query([watch(API, number=650), watch(API, number=12)])
        self.assertEqual(keymap, {"r0": {"p650": "acme/api#650", "p12": "acme/api#12"}})

    def test_bookkeeping_fields_never_change_the_document(self):
        plain = pr_poll.build_query([watch(API, number=650)])
        busy = watch(API, number=650, why="merge")
        busy["polledAt"] = "2026-10-24T08:05:00Z"
        busy["addedAt"] = "2026-10-20T00:00:00Z"
        self.assertEqual(pr_poll.build_query([busy]), plain)

    def test_repositories_alias_in_first_seen_order(self):
        _, keymap = pr_poll.build_query(
            [watch(WEB, number=7), watch(API, number=650), watch(WEB, number=8)]
        )
        self.assertEqual(
            keymap,
            {"r0": {"p7": "acme/web#7", "p8": "acme/web#8"}, "r1": {"p650": "acme/api#650"}},
        )

    def test_branch_aliases_count_within_their_repository(self):
        _, keymap = pr_poll.build_query(
            [watch(API, branch="a"), watch(WEB, branch="c"), watch(API, branch="b")]
        )
        self.assertEqual(
            keymap,
            {"r0": {"b0": "acme/api@a", "b1": "acme/api@b"}, "r1": {"b0": "acme/web@c"}},
        )

    def test_a_repeated_key_is_one_field(self):
        entries = [
            watch(API, number=650),
            watch(API, number=650, why="merge"),
            watch(API, branch="x"),
            watch(API, branch="x", why="branch"),
        ]
        query, keymap = pr_poll.build_query(entries)
        self.assertEqual(keymap, {"r0": {"p650": "acme/api#650", "b0": "acme/api@x"}})
        self.assertEqual(query.count("pullRequest("), 1)
        self.assertEqual(query.count("pullRequests("), 1)

    def test_an_entry_with_both_a_number_and_a_branch_polls_by_number(self):
        query, keymap = pr_poll.build_query([watch(API, number=650, branch="feat/x")])
        self.assertEqual(keymap, {"r0": {"p650": "acme/api#650"}})
        self.assertNotIn("feat/x", query)

    def test_legal_branch_names_pass_through(self):
        for branch in (
            "feat/pr-refs",
            "release/v0.33.0",
            "fix_under_scores",
            "a.b-c/d_e",
            "x" * 200,
        ):
            with self.subTest(branch=branch):
                query, keymap = pr_poll.build_query([watch(API, branch=branch)])
                self.assertIn('headRefName: "%s"' % branch, query)
                self.assertEqual(keymap, {"r0": {"b0": "acme/api@%s" % branch}})

    def test_owner_and_name_may_carry_dots_dashes_and_underscores(self):
        query, keymap = pr_poll.build_query([watch("my-org/my_repo.js", number=3)])
        self.assertIn('r0: repository(owner: "my-org", name: "my_repo.js") {', query)
        self.assertEqual(keymap, {"r0": {"p3": "my-org/my_repo.js#3"}})

    def test_the_largest_graphql_int_is_a_legal_number(self):
        query, _ = pr_poll.build_query([watch(API, number=2147483647)])
        self.assertIn("p2147483647: pullRequest(number: 2147483647)", query)

    def test_nothing_pollable_is_none_and_an_empty_keymap(self):
        for entries in (
            [],
            (),
            None,
            "acme/api#650",
            650,
            {"repo": API, "number": 1},
            [None, 5, "x", [], {}],
            [watch(API, number="1) { x")],
        ):
            with self.subTest(entries=entries):
                self.assertEqual(pr_poll.build_query(entries), (None, {}))

    def test_a_tuple_of_entries_works_like_a_list(self):
        self.assertEqual(pr_poll.build_query(tuple(TWO_REPOS)), pr_poll.build_query(TWO_REPOS))


class BuildQueryInjection(unittest.TestCase):
    """The values spliced into the document come from commands the session ran and from branch
    names, so a hostile one must cost the poll that one entry and the document nothing."""

    PARTS = (
        'x"',
        "x{",
        "x}",
        "x\ny",
        "x\n",
        "\nx",
        "x\r",
        "x\r\ny",
        'a" } } mutation { deleteRepository(input: {repositoryId: "x"}) { clientMutationId } } #',
        "x y",
        "x\\",
        'x\\"',
        "x$",
        "x`",
        "x;",
        "x(",
        "x)",
        "x#",
        "x@",
        "x:",
        "xé",
        "x\x00",
        "x y",
        "",
        " ",
        "\t",
    )

    def setUp(self):
        self.good = watch(WEB, number=7)
        self.good_built = pr_poll.build_query([self.good])

    def assert_leaves_no_trace(self, hostile):
        self.assertEqual(pr_poll.build_query([hostile]), (None, {}))
        self.assertEqual(pr_poll.build_query([hostile, self.good]), self.good_built)
        self.assertEqual(pr_poll.build_query([self.good, hostile]), self.good_built)
        self.assertIsNone(pr_poll.entry_key(hostile))

    def test_a_hostile_owner_is_skipped(self):
        for part in self.PARTS:
            with self.subTest(owner=part):
                self.assert_leaves_no_trace(watch(part + "/name", number=1))

    def test_a_hostile_name_is_skipped(self):
        for part in self.PARTS:
            with self.subTest(name=part):
                self.assert_leaves_no_trace(watch("owner/" + part, number=1))

    def test_a_hostile_branch_is_skipped(self):
        for part in self.PARTS:
            with self.subTest(branch=part):
                self.assert_leaves_no_trace(watch(API, branch=part))

    def test_a_trailing_newline_does_not_slip_past_the_anchors(self):
        # `$` matches before a final newline; the allow-lists must not.
        for hostile in (
            watch("acme\n/api", number=1),
            watch("acme/api\n", number=1),
            watch("acme/api", branch="main\n"),
            watch("acme/api", branch="feat/x\n"),
        ):
            with self.subTest(entry=hostile):
                self.assert_leaves_no_trace(hostile)

    def test_a_repo_that_is_not_exactly_owner_slash_name_is_skipped(self):
        for repo in ("a/b/c", "/n", "o/", "/", "", "o", None, 5, ["a", "b"], ("a", "b"), {"o": 1}):
            with self.subTest(repo=repo):
                self.assert_leaves_no_trace(watch(repo, number=1))

    def test_a_branch_outside_the_allow_list_is_skipped(self):
        for branch in ("x" * 201, "feat/x+y", "feat x", "café", "~x", "x^", "x:y", "x?", "x*"):
            with self.subTest(branch=branch):
                self.assert_leaves_no_trace(watch(API, branch=branch))

    def test_a_number_that_is_not_a_positive_int_is_skipped(self):
        for number in (
            "1) { x",
            "1) { x } } mutation { y",
            "650",
            "+650",
            "-1",
            -1,
            -650,
            0,
            True,
            False,
            1.0,
            1.5,
            float("nan"),
            float("inf"),
            [650],
            {"n": 650},
            2147483648,
            10**40,
        ):
            with self.subTest(number=number):
                self.assert_leaves_no_trace(watch(API, number=number))

    def test_a_bad_number_does_not_fall_back_to_a_good_branch(self):
        self.assert_leaves_no_trace(watch(API, number="1) { x", branch="feat/x"))
        self.assert_leaves_no_trace(watch(API, number=True, branch="feat/x"))

    def test_a_hostile_value_in_an_ignored_field_never_reaches_the_document(self):
        hostile = watch(API, number=650, why='x" } } mutation { y')
        hostile["polledAt"] = 'x" } mutation { y'
        hostile["extra"] = "\n}\nmutation {"
        query, keymap = pr_poll.build_query([hostile])
        self.assertEqual(
            (query, keymap), pr_poll.build_query([watch(API, number=650)])
        )
        self.assertNotIn("mutation", query)

    def test_entries_that_are_not_dicts_are_skipped(self):
        for junk in (None, "acme/api#650", 650, 6.5, True, ["repo", API], ("repo", API), b"x"):
            with self.subTest(entry=junk):
                self.assert_leaves_no_trace(junk)

    def test_whatever_is_skipped_the_document_stays_balanced(self):
        entries = [watch(part + "/name", number=1) for part in self.PARTS]
        entries += [watch(API, branch=part) for part in self.PARTS]
        entries += [self.good]
        query, _ = pr_poll.build_query(entries)
        self.assertEqual(query, self.good_built[0])
        self.assertEqual(query.count("{"), query.count("}"))
        self.assertEqual(query.count('"') % 2, 0)


class BuildQueryCap(unittest.TestCase):
    def numbered(self, count):
        return [watch(API if i % 2 == 0 else WEB, number=i + 1) for i in range(count)]

    def test_at_most_twenty_entries_go_into_one_query(self):
        entries = self.numbered(25)
        query, keymap = pr_poll.build_query(entries)
        self.assertEqual(keys_of(keymap), {pr_poll.entry_key(e) for e in entries[:20]})
        self.assertEqual(query.count("pullRequest("), 20)
        for late in entries[20:]:
            self.assertNotIn(pr_poll.entry_key(late), keys_of(keymap))

    def test_exactly_twenty_all_fit(self):
        entries = self.numbered(20)
        _, keymap = pr_poll.build_query(entries)
        self.assertEqual(len(keys_of(keymap)), 20)

    def test_the_cap_counts_entries_not_repositories_or_kinds(self):
        entries = []
        for i in range(30):
            repo = "acme/repo%d" % (i % 3)
            entries.append(watch(repo, branch="b%d" % i) if i % 2 else watch(repo, number=i + 1))
        query, keymap = pr_poll.build_query(entries)
        self.assertEqual(len(keys_of(keymap)), 20)
        self.assertEqual(keys_of(keymap), {pr_poll.entry_key(e) for e in entries[:20]})
        self.assertEqual(query.count("pullRequest(") + query.count("pullRequests("), 20)

    def test_skipped_and_repeated_entries_do_not_spend_the_cap(self):
        entries = []
        for i in range(30):
            entries.append(watch(API, number=i + 1))
            entries.append(watch(API, number=i + 1, why="merge"))  # a repeat of the same key
            entries.append(watch(API, number="1) { x%d" % i))  # unpollable
        _, keymap = pr_poll.build_query(entries)
        self.assertEqual(keys_of(keymap), {"acme/api#%d" % n for n in range(1, 21)})

    def test_the_cap_is_twenty(self):
        self.assertEqual(pr_poll.MAX_ENTRIES, 20)


class RunQuery(unittest.TestCase):
    QUERY = (
        'query { r0: repository(owner: "a", name: "b") { p1: pullRequest(number: 1) { number } } }'
    )

    def run_with(self, runner, **kwargs):
        return pr_poll.run_query(self.QUERY, runner=runner, **kwargs)

    def test_success_returns_the_parsed_response(self):
        body = {"data": {"r0": {"p1": {"number": 1}}}}
        self.assertEqual(self.run_with(FakeRunner(stdout=json.dumps(body))), body)

    def test_the_command_is_gh_api_graphql_with_the_query_as_one_string_field(self):
        runner = FakeRunner(stdout=answer({}))
        self.run_with(runner)
        self.assertEqual(len(runner.calls), 1)
        cmd, _ = runner.calls[0]
        self.assertEqual(cmd, ["gh", "api", "graphql", "-f", "query=" + self.QUERY])

    def test_gh_runs_with_no_stdin_captured_output_a_timeout_and_a_quiet_environment(self):
        runner = FakeRunner(stdout=answer({}))
        with mock.patch.dict(os.environ, {"GKS_TEST_SENTINEL": "kept"}):
            os.environ.pop("GH_PROMPT_DISABLED", None)
            os.environ.pop("GH_NO_UPDATE_NOTIFIER", None)
            self.run_with(runner)
            # the hook's own environment is left alone: the two variables go on a COPY
            self.assertNotIn("GH_PROMPT_DISABLED", os.environ)
            self.assertNotIn("GH_NO_UPDATE_NOTIFIER", os.environ)
        _, kwargs = runner.calls[0]
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], 4.0)
        self.assertEqual(kwargs["env"]["GH_PROMPT_DISABLED"], "1")
        self.assertEqual(kwargs["env"]["GH_NO_UPDATE_NOTIFIER"], "1")
        self.assertEqual(kwargs["env"]["GKS_TEST_SENTINEL"], "kept")

    def test_a_custom_timeout_is_passed_through(self):
        runner = FakeRunner(stdout=answer({}))
        self.run_with(runner, timeout=1.5)
        self.assertEqual(runner.calls[0][1]["timeout"], 1.5)

    def test_a_nonzero_exit_that_still_printed_data_is_kept(self):
        # gh exits 1 when GitHub answers `errors` beside `data`; one dead entry must not blind
        # the poll for the rest, so the partial answer survives.
        body = {
            "data": {"r0": {"p1": {"number": 1}, "p2": None}},
            "errors": [{"type": "NOT_FOUND", "path": ["r0", "p2"], "message": "no such PR"}],
        }
        runner = FakeRunner(
            stdout=json.dumps(body), returncode=1, stderr="gh: Could not resolve to a PullRequest"
        )
        self.assertEqual(self.run_with(runner), body)

    def test_a_nonzero_exit_with_an_empty_data_object_is_still_data(self):
        self.assertEqual(
            self.run_with(FakeRunner(stdout='{"data": {}}', returncode=1)), {"data": {}}
        )

    def test_a_nonzero_exit_without_data_is_none(self):
        for stdout in (
            '{"message": "Bad credentials", "documentation_url": "https://docs.github.com"}',
            '{"errors": [{"message": "boom"}]}',
            '{"data": null, "errors": [{"message": "boom"}]}',
            '{"data": []}',
            '{"data": "x"}',
            "[]",
            "",
            "gh: To get started with GitHub CLI, please run: gh auth login",
        ):
            with self.subTest(stdout=stdout):
                self.assertIsNone(self.run_with(FakeRunner(stdout=stdout, returncode=4)))

    def test_a_timeout_is_none(self):
        runner = FakeRunner(raises=subprocess.TimeoutExpired(["gh", "api"], 4.0))
        self.assertIsNone(self.run_with(runner))

    def test_a_missing_gh_is_none(self):
        self.assertIsNone(self.run_with(FakeRunner(raises=FileNotFoundError(2, "No such file"))))

    def test_other_os_level_failures_are_none_too(self):
        for error in (PermissionError(13, "Permission denied"), OSError(8, "Exec format error")):
            with self.subTest(error=error):
                self.assertIsNone(self.run_with(FakeRunner(raises=error)))

    def test_garbage_output_is_none(self):
        for stdout in ("", "not json", "{", '{"data": {', "[]", '"text"', "null", "42", "true"):
            with self.subTest(stdout=stdout):
                self.assertIsNone(self.run_with(FakeRunner(stdout=stdout)))

    def test_a_runner_that_answers_with_nonsense_is_none(self):
        for bogus in (lambda cmd, **kw: None, lambda cmd, **kw: object(), lambda cmd, **kw: "x"):
            with self.subTest(bogus=bogus):
                self.assertIsNone(self.run_with(bogus))

    def test_no_query_means_no_process(self):
        for query in (None, "", 5, ["query"], b"query"):
            with self.subTest(query=query):
                runner = FakeRunner(stdout=answer({}))
                self.assertIsNone(pr_poll.run_query(query, runner=runner))
                self.assertEqual(runner.calls, [])

    def test_without_a_runner_it_is_subprocess_run(self):
        runner = FakeRunner(stdout=answer({"r0": None}))
        with mock.patch.object(subprocess, "run", runner):
            self.assertEqual(pr_poll.run_query(self.QUERY), {"data": {"r0": None}})
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(runner.calls[0][0][:3], ["gh", "api", "graphql"])


class ParseResults(unittest.TestCase):
    def setUp(self):
        _, self.keymap = pr_poll.build_query(TWO_REPOS)
        self.data = json.loads(json.dumps(TWO_REPOS_DATA))  # a private copy to mutate

    def parse(self, data=None):
        return pr_poll.parse_results({"data": self.data if data is None else data}, self.keymap)

    def test_open_merged_and_closed_for_numbers_and_branches(self):
        self.assertEqual(self.parse(), TWO_REPOS_RESULTS)

    def test_the_bare_data_object_parses_the_same_as_the_full_response(self):
        self.assertEqual(pr_poll.parse_results(self.data, self.keymap), TWO_REPOS_RESULTS)

    def test_a_result_is_exactly_state_number_title_and_merged_at(self):
        for key, result in self.parse().items():
            with self.subTest(key=key):
                self.assertEqual(sorted(result), ["mergedAt", "number", "state", "title"])

    def test_merged_at_is_kept_only_as_a_non_empty_string(self):
        for raw, expected in (("2026-10-24T07:00:00Z", "2026-10-24T07:00:00Z"), ("", None),
                              (None, None), (12345, None)):
            with self.subTest(raw=raw):
                self.data["r1"]["p7"] = node(7, "MERGED", merged_at=raw)
                self.assertEqual(self.parse()["acme/web#7"]["mergedAt"], expected)

    def test_each_state_comes_through_as_github_spelled_it(self):
        for state in ("OPEN", "MERGED", "CLOSED"):
            with self.subTest(state=state):
                self.data["r1"]["p7"] = node(7, state)
                self.assertEqual(self.parse()["acme/web#7"]["state"], state)

    def test_a_null_pull_request_is_omitted(self):
        # a number that does not exist in the repository
        self.data["r0"]["p650"] = None
        results = self.parse()
        self.assertNotIn("acme/api#650", results)
        self.assertEqual(len(results), 3)

    def test_a_branch_without_nodes_is_omitted(self):
        for empty in ({"nodes": []}, {"nodes": [None]}, {"nodes": None}, {"nodes": "x"}, {}, None):
            with self.subTest(b0=empty):
                self.data["r0"]["b0"] = empty
                results = self.parse()
                self.assertNotIn("acme/api@feat/x", results)
                self.assertEqual(len(results), 3)

    def test_the_newest_node_wins_for_a_branch(self):
        self.data["r0"]["b0"] = {"nodes": [node(600, "MERGED", "Old"), node(651, "OPEN", "New")]}
        self.assertEqual(
            self.parse()["acme/api@feat/x"],
            {"state": "OPEN", "number": 651, "title": "New", "mergedAt": None},
        )

    def test_a_repository_alias_that_errored_drops_only_its_own_entries(self):
        self.data["r1"] = None
        results = self.parse()
        self.assertEqual(
            results, {k: v for k, v in TWO_REPOS_RESULTS.items() if k.startswith("acme/api")}
        )
        self.data["r1"] = TWO_REPOS_DATA["r1"]
        self.data["r0"] = None
        self.assertEqual(self.parse(), {"acme/web#7": TWO_REPOS_RESULTS["acme/web#7"]})

    def test_aliases_missing_from_the_answer_are_omitted(self):
        self.assertEqual(self.parse({}), {})
        self.assertEqual(self.parse({"r0": {}}), {})
        del self.data["r0"]["p12"]
        self.assertNotIn("acme/api#12", self.parse())

    def test_an_unknown_or_missing_state_is_omitted(self):
        for state in ("DRAFT", "open", "Merged", "", None, ["MERGED"], {"s": 1}, 3):
            with self.subTest(state=state):
                self.data["r1"]["p7"]["state"] = state
                self.assertNotIn("acme/web#7", self.parse())
        del self.data["r1"]["p7"]["state"]
        self.assertNotIn("acme/web#7", self.parse())

    def test_a_missing_or_bad_number_is_omitted(self):
        for number in (None, "7", True, 0, -7, 7.0, [7], 2147483648):
            with self.subTest(number=number):
                self.data["r1"]["p7"]["number"] = number
                self.assertNotIn("acme/web#7", self.parse())
        del self.data["r1"]["p7"]["number"]
        self.assertNotIn("acme/web#7", self.parse())

    def test_a_missing_or_odd_title_is_an_empty_string(self):
        for title in (None, 7, ["t"], {"t": 1}):
            with self.subTest(title=title):
                self.data["r1"]["p7"]["title"] = title
                self.assertEqual(self.parse()["acme/web#7"]["title"], "")
        del self.data["r1"]["p7"]["title"]
        self.assertEqual(self.parse()["acme/web#7"]["title"], "")

    def test_a_title_is_collapsed_to_one_line(self):
        # untrusted text that ends up inside a nudge: no newline may start a fake line there
        self.data["r1"]["p7"]["title"] = "  Fix\nthe\t\tthing \r\n  - injected line  "
        self.assertEqual(self.parse()["acme/web#7"]["title"], "Fix the thing - injected line")

    def test_garbage_in_gives_an_empty_result(self):
        for data, keymap in (
            (None, self.keymap),
            ("text", self.keymap),
            ([], self.keymap),
            (42, self.keymap),
            ({"data": None, "errors": [{"message": "boom"}]}, self.keymap),
            ({"data": {"r0": "weird", "r1": ["x"]}}, self.keymap),
            ({"data": {"r0": {"p650": ["x"], "p12": "x", "b0": 5}}}, self.keymap),
            ({"data": TWO_REPOS_DATA}, None),
            ({"data": TWO_REPOS_DATA}, []),
            ({"data": TWO_REPOS_DATA}, "r0"),
            ({"data": TWO_REPOS_DATA}, {}),
            ({"data": TWO_REPOS_DATA}, {"r0": "p650"}),
            ({"data": TWO_REPOS_DATA}, {"r0": None}),
        ):
            with self.subTest(data=data, keymap=keymap):
                self.assertEqual(pr_poll.parse_results(data, keymap), {})


class Poll(unittest.TestCase):
    def test_end_to_end_with_a_fake_runner(self):
        hostile = watch(API, number="1) { x", branch="feat/x")
        entries = TWO_REPOS + [hostile, watch(API, number=650, why="merge")]
        runner = FakeRunner(stdout=answer(TWO_REPOS_DATA))
        self.assertEqual(pr_poll.poll(entries, runner=runner), TWO_REPOS_RESULTS)
        self.assertEqual(len(runner.calls), 1)
        cmd, kwargs = runner.calls[0]
        self.assertEqual(cmd, ["gh", "api", "graphql", "-f", "query=" + TWO_REPOS_QUERY])
        self.assertEqual(kwargs["timeout"], 4.0)

    def test_the_timeout_reaches_the_runner(self):
        runner = FakeRunner(stdout=answer(TWO_REPOS_DATA))
        pr_poll.poll(TWO_REPOS, timeout=2.5, runner=runner)
        self.assertEqual(runner.calls[0][1]["timeout"], 2.5)

    def test_partial_errors_keep_what_was_answered(self):
        data = {"r0": TWO_REPOS_DATA["r0"], "r1": None}
        errors = [{"type": "NOT_FOUND", "path": ["r1"], "message": "no such repository"}]
        runner = FakeRunner(stdout=answer(data, errors), returncode=1)
        expected = {k: v for k, v in TWO_REPOS_RESULTS.items() if k.startswith("acme/api")}
        self.assertEqual(pr_poll.poll(TWO_REPOS, runner=runner), expected)

    def test_a_branch_that_merged_is_reported_with_its_number(self):
        entries = [watch(API, branch="feat/x", why="merge")]
        data = {"r0": {"b0": {"nodes": [node(651, "MERGED", "Next step", "2026-10-24T09:00:00Z")]}}}
        self.assertEqual(
            pr_poll.poll(entries, runner=FakeRunner(stdout=answer(data))),
            {
                "acme/api@feat/x": {
                    "state": "MERGED",
                    "number": 651,
                    "title": "Next step",
                    "mergedAt": "2026-10-24T09:00:00Z",
                }
            },
        )

    def test_every_way_gh_can_fail_is_an_empty_dict(self):
        for runner in (
            FakeRunner(raises=FileNotFoundError(2, "No such file")),
            FakeRunner(raises=subprocess.TimeoutExpired(["gh"], 4.0)),
            FakeRunner(raises=PermissionError(13, "Permission denied")),
            FakeRunner(stdout='{"message": "Bad credentials"}', returncode=1),
            FakeRunner(stdout="", returncode=4, stderr="gh: To get started, run: gh auth login"),
            FakeRunner(stdout="not json"),
            FakeRunner(stdout="[]"),
            FakeRunner(stdout=answer(None)),
            FakeRunner(stdout=answer({})),
        ):
            with self.subTest(runner=vars(runner)):
                self.assertEqual(pr_poll.poll(TWO_REPOS, runner=runner), {})
                self.assertEqual(len(runner.calls), 1)

    def test_nothing_pollable_never_starts_a_process(self):
        for entries in ([], None, "x", [watch(API, number="1) { x")], [watch("a/b/c", number=1)]):
            with self.subTest(entries=entries):
                runner = FakeRunner(stdout=answer(TWO_REPOS_DATA))
                self.assertEqual(pr_poll.poll(entries, runner=runner), {})
                self.assertEqual(runner.calls, [])

    def test_a_bug_anywhere_inside_is_still_an_empty_dict(self):
        for name in ("build_query", "run_query", "parse_results"):
            with self.subTest(raises_in=name):
                with mock.patch.object(pr_poll, name, side_effect=RuntimeError("boom")):
                    runner = FakeRunner(stdout=answer(TWO_REPOS_DATA))
                    self.assertEqual(pr_poll.poll(TWO_REPOS, runner=runner), {})

    def test_it_prints_nothing_whatever_happens(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            pr_poll.poll(TWO_REPOS, runner=FakeRunner(stdout=answer(TWO_REPOS_DATA)))
            pr_poll.poll(TWO_REPOS, runner=FakeRunner(raises=FileNotFoundError(2, "No such file")))
            pr_poll.poll(TWO_REPOS, runner=FakeRunner(stdout="not json"))
            pr_poll.poll(None)
            pr_poll.poll([watch(API, number="1) { x")])
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")


class RealSubprocess(unittest.TestCase):
    """The same poll against a REAL child process: a fake `gh` script is placed first on PATH, so
    the argv, the environment, stdin, the exit-code handling and the timeout are exercised through
    subprocess.run itself rather than through an injected stand-in."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-pr-poll-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.bin_dir = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.bin_dir)
        self.argv_file = os.path.join(self.tmpdir, "argv")
        self.env_file = os.path.join(self.tmpdir, "env")
        self.stdin_file = os.path.join(self.tmpdir, "stdin")
        self.response_file = os.path.join(self.tmpdir, "response.json")
        patcher = mock.patch.dict(
            os.environ, {"PATH": self.bin_dir + os.pathsep + os.environ.get("PATH", "")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_gh(self, body):
        path = os.path.join(self.bin_dir, "gh")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n%s\n" % body)
        os.chmod(path, 0o755)

    def fake_gh_recording_and_answering(self, exit_code=0):
        """Record what gh was handed (NUL-separated argv, the two env switches, whatever arrived
        on stdin), then print the canned response file."""
        self.fake_gh(
            "printf '%%s\\000' \"$@\" > \"%(argv)s\"\n"
            "printf '%%s|%%s' \"${GH_PROMPT_DISABLED-unset}\" \"${GH_NO_UPDATE_NOTIFIER-unset}\""
            ' > "%(env)s"\n'
            'cat > "%(stdin)s"\n'
            'cat "%(response)s"\n'
            "exit %(code)d"
            % {
                "argv": self.argv_file,
                "env": self.env_file,
                "stdin": self.stdin_file,
                "response": self.response_file,
                "code": exit_code,
            }
        )

    def write_response(self, data, errors=None):
        with open(self.response_file, "w", encoding="utf-8") as fh:
            fh.write(answer(data, errors))

    def read(self, path):
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8")

    def test_a_fake_gh_on_path_answers_through_the_real_subprocess_path(self):
        self.write_response(TWO_REPOS_DATA)
        self.fake_gh_recording_and_answering()
        self.assertEqual(pr_poll.poll(TWO_REPOS), TWO_REPOS_RESULTS)
        # gh was handed the documented command line, the whole document in ONE argv element
        self.assertEqual(
            self.read(self.argv_file).split("\0")[:-1],
            ["api", "graphql", "-f", "query=" + TWO_REPOS_QUERY],
        )
        # the two switches that keep gh from prompting or checking for updates
        self.assertEqual(self.read(self.env_file), "1|1")
        # stdin was /dev/null: gh saw end-of-file, not the hook's input
        self.assertEqual(self.read(self.stdin_file), "")

    def test_a_nonzero_exit_with_data_is_a_partial_answer_not_a_failure(self):
        self.write_response(
            {"r0": TWO_REPOS_DATA["r0"], "r1": None},
            [{"type": "NOT_FOUND", "path": ["r1"], "message": "no such repository"}],
        )
        self.fake_gh_recording_and_answering(exit_code=1)
        expected = {k: v for k, v in TWO_REPOS_RESULTS.items() if k.startswith("acme/api")}
        self.assertEqual(pr_poll.poll(TWO_REPOS), expected)

    def test_a_gh_that_fails_without_data_learns_nothing(self):
        self.fake_gh("echo 'gh: To get started, please run:  gh auth login' >&2\nexit 4")
        self.assertEqual(pr_poll.poll(TWO_REPOS), {})

    def test_no_gh_on_path_learns_nothing(self):
        empty = os.path.join(self.tmpdir, "empty")
        os.makedirs(empty)
        with mock.patch.dict(os.environ, {"PATH": empty}):
            self.assertIsNone(shutil.which("gh"))
            self.assertEqual(pr_poll.poll(TWO_REPOS), {})

    def test_a_hung_gh_is_cut_off_at_the_timeout(self):
        # `exec` so the process that is killed is the sleep itself and no orphan holds a pipe open.
        self.fake_gh("exec sleep 30")
        started = time.monotonic()
        results = pr_poll.poll(TWO_REPOS, timeout=0.5)
        elapsed = time.monotonic() - started
        self.assertEqual(results, {})
        self.assertLess(elapsed, 10, msg="poll took %.1fs with a hung gh" % elapsed)


if __name__ == "__main__":
    unittest.main()
