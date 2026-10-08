#!/usr/bin/env python3
"""Unit tests for routing-guard.sh / routing_guard.py, the gitian-kb plugin's PreToolUse
routing-precondition guard.

Drives it end to end via `sh routing-guard.sh` (matching how hooks.json invokes it, on matcher
"mcp__(plugin_gitian-kb_)?[gG][iI][tT][iI][aA][nN]-[kK][bB]__(publish_doc|publish_entry|append_entry|batch_write)").
The guard keeps no state of its own and reads
exactly one field of the nudge layer's -- the cached connection default -- so every test points
GITIAN_KB_STATE_FILE at a throwaway path, and a real throwaway git repo is created per test that
needs one, so nothing reads the developer's own state file or checkout.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_routing_guard.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
ROUTING_GUARD_SH = HOOKS_DIR / "routing-guard.sh"
HARVEST_SH = HOOKS_DIR / "harvest.sh"

SERVER_KEY = "https://gitian.dev/api/mcp/kb"  # default GITIAN_KB_URL, per the state contract

# The code server's five tools under both spellings (the gitian-docs plugin's `gitian-code` key, and a
# hand-wired `gitian-code`). Every hook in this plugin is about the KB server, so none of these may
# ever be seen as one of its calls -- even though the names contain "gitian" and `code_search` ends
# in a read suffix.
CODE_TOOLS = ("code_repos", "code_overview", "code_search", "code_annotation", "code_page")
CODE_PREFIXES = ("mcp__plugin_gitian-docs_gitian-code__", "mcp__gitian-code__")
CODE_TOOL_NAMES = tuple(prefix + tool for prefix in CODE_PREFIXES for tool in CODE_TOOLS)

GITIAN_TOOL = "mcp__plugin_gitian-kb_gitian-kb__append_entry"
BATCH_TOOL = "mcp__plugin_gitian-kb_gitian-kb__batch_write"


def envelope(tool_name=GITIAN_TOOL, tool_input=None, cwd="/tmp", session_id="sess-1"):
    return {
        "session_id": session_id,
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {"section": "x"},
    }


class RoutingGuardTestCase(unittest.TestCase):
    def setUp(self):
        # The guard reads ONE thing from the nudge layer's state file -- the cached connection
        # default -- so every test points it at a throwaway path (absent = nothing cached) rather
        # than at the developer's own ~/.claude/gitian-kb/state.json.
        self.state_dir = tempfile.mkdtemp(prefix="gks-routing-state-")
        self.state_file = os.path.join(self.state_dir, "state.json")
        self.env = dict(os.environ)
        self.env["GITIAN_KB_STATE_FILE"] = self.state_file
        self.env.pop("GITIAN_KB_URL", None)
        self.repos = []

    def tearDown(self):
        shutil.rmtree(self.state_dir, ignore_errors=True)
        for path in self.repos:
            shutil.rmtree(path, ignore_errors=True)

    def cache_default_kb(self, default_kb, server_key=SERVER_KEY):
        """Write the state file the way harvest.py leaves it after a vocabulary read."""
        state = {
            "schemaVersion": 1,
            "servers": {server_key: {"defaultKb": default_kb}},
            "sessions": {},
        }
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh)

    def make_repo(self, remote=None):
        """A throwaway git repo, optionally with an `origin` remote. `git init` needs no user
        config, so this works on a host with no global git identity."""
        path = tempfile.mkdtemp(prefix="gks-routing-repo-")
        self.repos.append(path)
        subprocess.run(["git", "init", "-q", path], check=True)
        if remote is not None:
            subprocess.run(["git", "-C", path, "remote", "add", "origin", remote], check=True)
        return path

    def make_plain_dir(self):
        path = tempfile.mkdtemp(prefix="gks-routing-plain-")
        self.repos.append(path)
        return path

    def run_hook(self, payload, env=None):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(ROUTING_GUARD_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=env if env is not None else self.env,
            timeout=15,
        )

    def assert_allow(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stdout=%r stderr=%r" % (proc.stdout, proc.stderr))
        self.assertEqual(proc.stdout, "", msg="expected silence, got %r" % proc.stdout)

    def assert_deny(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        return out["permissionDecisionReason"]


class DeniesUnroutableWrite(RoutingGuardTestCase):
    def test_no_kb_and_no_repo_in_a_github_repo_denies_and_names_the_repo(self):
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        reason = self.assert_deny(self.run_hook(envelope(cwd=repo_dir)))
        self.assertIn('`repo: "acme/widgets"`', reason)
        self.assertIn("no `kb` and no `repo`", reason)
        self.assertIn("nothing to route on", reason)
        self.assertIn("`landed_in`", reason)
        self.assertIn('`"home"`', reason)
        # The deny has to be actionable inside a subagent too: it has a brief, not a cwd.
        self.assertIn("brief", reason)

    def test_denies_deterministically_on_an_identical_resend(self):
        # Unlike the once-per-session nudges, the precondition is still unmet on a re-send.
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        for _ in range(3):
            self.assert_deny(self.run_hook(envelope(cwd=repo_dir)))

    def test_denies_for_each_routed_write_tool(self):
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        for tool in (
            "mcp__plugin_gitian-kb_gitian-kb__publish_doc",
            "mcp__plugin_gitian-kb_gitian-kb__publish_entry",
            "mcp__plugin_gitian-kb_gitian-kb__append_entry",
            "mcp__gitian-kb__append_entry",  # the other server alias shape hooks already handle
        ):
            self.assert_deny(self.run_hook(envelope(tool_name=tool, cwd=repo_dir)))

    def test_blank_and_null_fields_count_as_absent(self):
        repo_dir = self.make_repo("https://github.com/acme/widgets")
        for tool_input in (
            {"kb": "", "repo": ""},
            {"kb": None, "repo": None},
            {"repo": "   "},
            {"kb": "  "},
        ):
            self.assert_deny(self.run_hook(envelope(tool_input=tool_input, cwd=repo_dir)))


class RemoteForms(RoutingGuardTestCase):
    def test_ssh_https_and_dot_git_forms_all_normalize_to_owner_name(self):
        for remote in (
            "git@github.com:acme/widgets.git",
            "git@github.com:acme/widgets",
            "ssh://git@github.com/acme/widgets.git",
            "https://github.com/acme/widgets.git",
            "https://github.com/acme/widgets",
            "https://github.com/acme/widgets/",
            "https://user@github.com/acme/widgets.git",
            # A non-`git` scp-form user: a deploy key or a self-hosted forge. This is exactly what
            # the broadened `_SCP_FORM_RE` buys over matching `git@` alone.
            "deploy@github.com:acme/widgets.git",
        ):
            repo_dir = self.make_repo(remote)
            reason = self.assert_deny(self.run_hook(envelope(cwd=repo_dir)))
            self.assertIn('`repo: "acme/widgets"`', reason, msg="remote=%s" % remote)

    def test_a_remote_that_does_not_reduce_to_one_pair_fails_open(self):
        # Subgroup paths and host-only URLs aren't safe to hand back as `repo` -- same rule
        # session-context.sh applies, so the guard stays silent rather than suggesting a bad value.
        for remote in (
            "https://gitlab.com/group/subgroup/widgets.git",
            "git@gitlab.com:group/subgroup/widgets.git",
            "https://github.com/",
            "https://github.com/acme",
        ):
            repo_dir = self.make_repo(remote)
            self.assert_allow(self.run_hook(envelope(cwd=repo_dir)))


class AllowsEverythingElse(RoutingGuardTestCase):
    def test_explicit_kb_allows(self):
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        self.assert_allow(self.run_hook(envelope(tool_input={"kb": "acme/team"}, cwd=repo_dir)))
        self.assert_allow(self.run_hook(envelope(tool_input={"kb": "home"}, cwd=repo_dir)))

    def test_explicit_repo_allows(self):
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        self.assert_allow(self.run_hook(envelope(tool_input={"repo": "acme/widgets"}, cwd=repo_dir)))

    def test_never_routed_tools_are_never_denied(self):
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        for tool in (
            "mcp__plugin_gitian-kb_gitian-kb__publish_memory",
            "mcp__plugin_gitian-kb_gitian-kb__patch_doc",
            "mcp__plugin_gitian-kb_gitian-kb__patch_memory",
            "mcp__plugin_gitian-kb_gitian-kb__retract_item",
            "mcp__plugin_gitian-kb_gitian-kb__publish_topic",
            "mcp__plugin_gitian-kb_gitian-kb__retract_topic",
            "mcp__plugin_gitian-kb_gitian-kb__search",
        ):
            self.assert_allow(self.run_hook(envelope(tool_name=tool, cwd=repo_dir)))

    def test_a_non_gitian_server_is_never_denied(self):
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        self.assert_allow(
            self.run_hook(envelope(tool_name="mcp__other__append_entry", cwd=repo_dir))
        )
        self.assert_allow(self.run_hook(envelope(tool_name="mcp__other__publish_doc", cwd=repo_dir)))
        self.assert_allow(self.run_hook(envelope(tool_name="Edit", cwd=repo_dir)))
        self.assert_allow(self.run_hook(envelope(tool_name="Write", cwd=repo_dir)))


class FailOpen(RoutingGuardTestCase):
    def test_not_a_git_repo_is_silent(self):
        self.assert_allow(self.run_hook(envelope(cwd=self.make_plain_dir())))

    def test_a_repo_with_no_origin_remote_is_silent(self):
        self.assert_allow(self.run_hook(envelope(cwd=self.make_repo())))

    def test_missing_cwd_is_silent(self):
        payload = envelope()
        del payload["cwd"]
        self.assert_allow(self.run_hook(payload))

    def test_nonexistent_cwd_is_silent(self):
        self.assert_allow(self.run_hook(envelope(cwd="/definitely/not/a/directory/anywhere")))

    def test_empty_garbage_and_non_object_stdin_are_silent(self):
        for raw in ("", "   ", "{not valid json ][ at all", "[]", "[1, 2, 3]", "null"):
            self.assert_allow(self.run_hook(raw))

    def test_missing_or_malformed_tool_input_is_silent(self):
        # A `tool_input` that isn't an object gives the guard nothing to inspect, so it allows
        # rather than denying on an assumption about a payload shape it doesn't recognize.
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        payload = envelope(cwd=repo_dir)
        del payload["tool_input"]
        self.assert_allow(self.run_hook(payload))
        self.assert_allow(self.run_hook(envelope(tool_input="not-an-object", cwd=repo_dir)))
        self.assert_allow(self.run_hook(envelope(tool_input=[1, 2], cwd=repo_dir)))
        self.assert_allow(self.run_hook(envelope(tool_name=None, cwd=repo_dir)))

    def test_a_non_string_kb_or_repo_counts_as_present_and_allows(self):
        # A wrongly-typed `kb`/`repo` is the server's validation error to report, not this hook's
        # to pre-empt: denying here would replace a precise schema failure with a routing lecture.
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        for tool_input in ({"kb": 7}, {"repo": ["acme/widgets"]}, {"kb": {"slug": "home"}}):
            self.assert_allow(self.run_hook(envelope(tool_input=tool_input, cwd=repo_dir)))

    def test_no_git_binary_on_path_is_silent(self):
        # `git` unresolvable makes the subprocess call raise FileNotFoundError; the guard must
        # allow rather than crash. Driven through the python entry point with an absolute
        # interpreter, since narrowing PATH would also make `sh` itself unresolvable.
        repo_dir = self.make_repo("git@github.com:acme/widgets.git")
        empty_bin = tempfile.mkdtemp(prefix="gks-routing-nobin-")
        self.repos.append(empty_bin)
        env = dict(self.env)
        env["PATH"] = empty_bin
        proc = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "routing_guard.py")],
            input=json.dumps(envelope(cwd=repo_dir)),
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_missing_guard_script_is_silent(self):
        # The wrapper's own guard: routing_guard.py absent -> exit 0, no output.
        sandbox = tempfile.mkdtemp(prefix="gks-routing-lonely-")
        self.repos.append(sandbox)
        lonely = os.path.join(sandbox, "routing-guard.sh")
        shutil.copy(str(ROUTING_GUARD_SH), lonely)
        proc = subprocess.run(
            ["sh", lonely],
            input=json.dumps(envelope(cwd=self.make_repo("git@github.com:acme/widgets.git"))),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


class ConnectionDefaultStandsTheGuardDown(RoutingGuardTestCase):
    """A human-chosen connection default (`default_kb.source == "connection"`, not unavailable)
    means a kb-less write has a deliberate destination -- the hosted client with no checkout the
    setting exists for. Every other cache state leaves the refusal exactly as it was."""

    def setUp(self):
        super().setUp()
        self.repo_dir = self.make_repo("git@github.com:acme/widgets.git")

    def test_allows_a_kb_less_repo_less_write_when_the_default_was_chosen(self):
        self.cache_default_kb({"kb": "support", "source": "connection"})
        for tool in (
            "mcp__plugin_gitian-kb_gitian-kb__publish_doc",
            "mcp__plugin_gitian-kb_gitian-kb__publish_entry",
            "mcp__plugin_gitian-kb_gitian-kb__append_entry",
            "mcp__gitian-kb__append_entry",
        ):
            self.assert_allow(self.run_hook(envelope(tool_name=tool, cwd=self.repo_dir)))

    def test_regression_a_home_default_still_refuses(self):
        # `home` is what the connection falls back to when nobody chose anything: that is the
        # silent fork the guard exists for.
        self.cache_default_kb({"kb": "home", "source": "home"})
        self.assert_deny(self.run_hook(envelope(cwd=self.repo_dir)))

    def test_regression_a_chosen_default_that_is_unavailable_still_refuses(self):
        # The configured KB is gone and writes now fall back to `home` -- not a deliberate landing.
        self.cache_default_kb({"kb": "home", "source": "connection", "unavailable": "Support"})
        self.assert_deny(self.run_hook(envelope(cwd=self.repo_dir)))

    def test_no_cache_at_all_still_refuses(self):
        self.assertFalse(os.path.exists(self.state_file))
        self.assert_deny(self.run_hook(envelope(cwd=self.repo_dir)))

    def test_a_default_cached_for_another_server_does_not_count(self):
        self.cache_default_kb(
            {"kb": "support", "source": "connection"}, server_key="https://other.example/api/mcp/kb"
        )
        self.assert_deny(self.run_hook(envelope(cwd=self.repo_dir)))

    def test_the_cache_is_looked_up_under_the_overridden_server_url(self):
        self.cache_default_kb(
            {"kb": "support", "source": "connection"}, server_key="https://kb.example/api/mcp/kb"
        )
        env = dict(self.env)
        env["GITIAN_KB_URL"] = "https://kb.example"
        self.assert_allow(self.run_hook(envelope(cwd=self.repo_dir), env=env))
        # ...and the default URL's empty slot is still refused.
        self.assert_deny(self.run_hook(envelope(cwd=self.repo_dir)))

    def test_an_unreadable_or_malformed_cache_degrades_to_the_original_refusal(self):
        # Fail open means "never block on a hiccup"; it does not mean "allow whenever the cache is
        # confusing". A malformed cache cannot prove the destination is deliberate.
        for raw in ("{not json ][", "[]", '{"servers": []}', '{"servers": {"%s": 3}}' % SERVER_KEY):
            with open(self.state_file, "w", encoding="utf-8") as fh:
                fh.write(raw)
            self.assert_deny(self.run_hook(envelope(cwd=self.repo_dir)))
        for bad in ("support", ["connection"], {"source": ["connection"]}, {"source": "Connection"}):
            self.cache_default_kb(bad)
            self.assert_deny(self.run_hook(envelope(cwd=self.repo_dir)))

    def test_an_explicit_kb_or_repo_is_allowed_whatever_the_cache_says(self):
        self.cache_default_kb({"kb": "home", "source": "home"})
        self.assert_allow(self.run_hook(envelope(tool_input={"kb": "home"}, cwd=self.repo_dir)))
        self.assert_allow(
            self.run_hook(envelope(tool_input={"repo": "acme/widgets"}, cwd=self.repo_dir))
        )

    def test_end_to_end_a_vocab_read_through_harvest_is_what_arms_the_allowance(self):
        # The producer and the consumer are two scripts; this is the one test that proves they
        # agree on the key and the shape, using the bare-list payload a live hook receives.
        resource = {
            "uri": "gitian-kb://vocab",
            "mimeType": "application/json",
            "text": json.dumps(
                {
                    "kb": "support",
                    "default_kb": {"kb": "support", "source": "connection"},
                    "topics": [],
                }
            ),
        }
        harvest_payload = {
            "session_id": "sess-1",
            "cwd": "/repo",
            "hook_event_name": "PostToolUse",
            "tool_name": "mcp__plugin_gitian-kb_gitian-kb__read_resource",
            "tool_input": {"uri": "gitian-kb://vocab"},
            "tool_response": [{"type": "text", "text": json.dumps(resource)}],
        }
        self.assert_deny(self.run_hook(envelope(cwd=self.repo_dir)))
        proc = subprocess.run(
            ["sh", str(HARVEST_SH)],
            input=json.dumps(harvest_payload),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assert_allow(self.run_hook(envelope(cwd=self.repo_dir)))


def batch_envelope(operations, tool_name=BATCH_TOOL, cwd="/tmp"):
    """A PreToolUse payload for a batch_write: `operations` is `[{tool, args}]`, tool names bare."""
    return envelope(tool_name=tool_name, tool_input={"operations": operations}, cwd=cwd)


def op(tool, **args):
    return {"tool": tool, "args": args}


class BatchWriteOperations(RoutingGuardTestCase):
    """A batch_write runs its operations through the same per-tool branches as standalone calls,
    so an operation with neither `kb` nor `repo` forks into `home` exactly like the standalone call
    this guard already refuses -- it must not be the way round the guard. The batch has no
    top-level `kb`; each operation resolves its own."""

    def setUp(self):
        super().setUp()
        self.repo_dir = self.make_repo("git@github.com:acme/widgets.git")

    def run_batch(self, operations, **kwargs):
        return self.run_hook(batch_envelope(operations, cwd=self.repo_dir, **kwargs))

    def test_regression_hooks_json_routes_batch_write_to_the_guard_and_the_lint(self):
        # The gap this closes: both PreToolUse matchers named the standalone tools only, so a
        # batch_write never reached either script however they handled operations.
        hooks = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
        matchers = {
            entry["hooks"][0]["command"].rsplit("/", 1)[-1].rstrip('"'): entry["matcher"]
            for entry in hooks["hooks"]["PreToolUse"]
        }
        for script in ("routing-guard.sh", "publish-lint.sh"):
            self.assertTrue(
                re.search(matchers[script], BATCH_TOOL), msg="%s matcher misses batch_write" % script
            )
            self.assertTrue(re.search(matchers[script], "mcp__gitian-kb__batch_write"))
        # A category carries no topics and no routing, so neither script matches it.
        for script in ("routing-guard.sh", "publish-lint.sh"):
            self.assertFalse(
                re.search(matchers[script], "mcp__plugin_gitian-kb_gitian-kb__publish_category")
            )

    def test_denies_naming_the_offending_operation_index_and_its_tool(self):
        reason = self.assert_deny(
            self.run_batch(
                [
                    op("publish_doc", slug="a", kb="acme/team"),
                    op("append_entry", section="more"),
                    op("publish_memory", slug="m"),
                ]
            )
        )
        self.assertIn("operation 1 (append_entry)", reason)
        self.assertNotIn("0 (", reason)
        self.assertNotIn("2 (", reason)
        self.assertIn('`repo: "acme/widgets"`', reason)
        self.assertIn("Nothing in the batch ran", reason)
        self.assertIn("re-send the batch", reason)

    def test_names_every_offending_operation_when_several_lack_both(self):
        reason = self.assert_deny(
            self.run_batch(
                [
                    op("publish_entry", scope="work"),
                    op("publish_doc", slug="ok", repo="acme/widgets"),
                    op("publish_doc", slug="bad"),
                ]
            )
        )
        self.assertIn("operations 0 (publish_entry), 2 (publish_doc)", reason)

    def test_a_long_offender_list_is_capped_and_counted(self):
        reason = self.assert_deny(self.run_batch([op("append_entry") for _ in range(13)]))
        self.assertIn("9 (append_entry), and 3 more", reason)
        self.assertNotIn("10 (", reason)

    def test_denies_under_both_server_spellings_and_deterministically(self):
        operations = [op("publish_doc", slug="a")]
        for tool_name in (BATCH_TOOL, "mcp__gitian-kb__batch_write"):
            for _ in range(2):
                self.assert_deny(self.run_batch(operations, tool_name=tool_name))

    def test_blank_and_null_args_count_as_absent(self):
        for args in ({"kb": "", "repo": ""}, {"kb": None, "repo": None}, {"repo": "  "}):
            self.assert_deny(self.run_batch([{"tool": "append_entry", "args": args}]))

    def test_allows_when_every_covered_operation_names_a_kb_or_a_repo(self):
        self.assert_allow(
            self.run_batch(
                [
                    op("publish_doc", slug="a", kb="home"),
                    op("publish_entry", scope="work", repo="acme/widgets"),
                    op("append_entry", kb="acme/team", section="x"),
                ]
            )
        )

    def test_operations_of_other_tools_are_not_this_guards_business(self):
        # Memories never route, patches revise an existing item in its own KB, and an unknown or
        # nested tool is refused by the server itself.
        self.assert_allow(
            self.run_batch(
                [
                    op("publish_memory", slug="m"),
                    op("patch_doc", slug="d", status="landed"),
                    op("patch_memory", slug="m", summary="s"),
                    op("retract_item", slug="x"),
                    op("batch_write", operations=[]),
                    op("mcp__gitian-kb__publish_doc", slug="namespaced"),
                    op("nonsense"),
                ]
            )
        )

    def test_malformed_operations_fail_open_and_never_hide_a_real_offender(self):
        for tool_input in (
            {},
            {"operations": None},
            {"operations": "publish_doc"},
            {"operations": {"tool": "publish_doc", "args": {}}},
            {"operations": []},
            {"operations": [None, "x", 3, [], {}]},
            {"operations": [{"tool": "publish_doc"}, {"tool": "publish_doc", "args": "x"}]},
            {"operations": [{"tool": None, "args": {}}, {"args": {}}]},
        ):
            self.assert_allow(
                self.run_hook(
                    envelope(tool_name=BATCH_TOOL, tool_input=tool_input, cwd=self.repo_dir)
                )
            )
        # Garbage around a genuine offender does not shield it, and the index is its real one.
        reason = self.assert_deny(
            self.run_batch([None, {"tool": "publish_doc"}, "x", op("append_entry", section="s")])
        )
        self.assertIn("operation 3 (append_entry)", reason)

    def test_a_top_level_kb_or_repo_on_the_batch_does_not_satisfy_its_operations(self):
        # The batch schema takes neither; each operation resolves its own, so neither is evidence
        # about where an operation lands.
        self.assert_deny(
            self.run_hook(
                envelope(
                    tool_name=BATCH_TOOL,
                    tool_input={
                        "kb": "acme/team",
                        "repo": "acme/widgets",
                        "operations": [op("append_entry", section="s")],
                    },
                    cwd=self.repo_dir,
                )
            )
        )

    def test_a_non_gitian_server_batch_write_is_never_denied(self):
        self.assert_allow(
            self.run_batch([op("publish_doc", slug="a")], tool_name="mcp__other__batch_write")
        )

    def test_fails_open_without_a_github_origin_like_the_standalone_guard(self):
        operations = [op("publish_doc", slug="a")]
        self.assert_allow(
            self.run_hook(batch_envelope(operations, cwd=self.make_plain_dir()))
        )
        self.assert_allow(
            self.run_hook(batch_envelope(operations, cwd=self.make_repo("https://github.com/acme")))
        )
        self.assert_allow(
            self.run_hook(
                envelope(tool_name=BATCH_TOOL, tool_input={"operations": operations}, cwd="")
            )
        )

    def test_a_chosen_connection_default_stands_the_guard_down_for_a_batch_too(self):
        operations = [op("publish_doc", slug="a"), op("append_entry", section="s")]
        self.cache_default_kb({"kb": "support", "source": "connection"})
        self.assert_allow(self.run_batch(operations))
        # ...and every other cache state keeps the refusal.
        self.cache_default_kb({"kb": "home", "source": "home"})
        self.assert_deny(self.run_batch(operations))
        self.cache_default_kb({"kb": "gone", "source": "connection", "unavailable": True})
        self.assert_deny(self.run_batch(operations))

    def test_the_standalone_reason_is_unchanged(self):
        # Factoring the remedy out for the batch wording must not move a word of the original.
        reason = self.assert_deny(self.run_hook(envelope(cwd=self.repo_dir)))
        self.assertTrue(
            reason.startswith(
                "gitian-kb routing guard: this write names no `kb` and no `repo`, so it has "
                "nothing to route on and can only land in `home` -- in an org checkout that "
                "silently forks the team's KB. Set one of them and re-send:\n- `repo: "
            )
        )
        self.assertNotIn("batch", reason)


class CodeServerIgnored(RoutingGuardTestCase):
    """The guard is about the KB's write tools. The code server's tools are read-only and never its
    business -- and the proof has to be about the SERVER, not about a tool name that happens to miss
    a marker, so the control case sends the very same write tools under the code server's names."""

    def setUp(self):
        super().setUp()
        self.repo_dir = self.make_repo("git@github.com:acme/widgets.git")

    def test_every_code_tool_is_allowed_in_a_github_checkout(self):
        for tool_name in CODE_TOOL_NAMES:
            with self.subTest(tool_name=tool_name):
                self.assert_allow(
                    self.run_hook(
                        envelope(
                            tool_name=tool_name,
                            tool_input={"repo": "acme/api", "query": "session"},
                            cwd=self.repo_dir,
                        )
                    )
                )

    def test_a_write_tool_name_on_the_code_server_is_still_not_the_kbs(self):
        for prefix in CODE_PREFIXES:
            for tool in ("publish_doc", "publish_entry", "append_entry", "batch_write"):
                with self.subTest(tool_name=prefix + tool):
                    self.assert_allow(
                        self.run_hook(
                            envelope(tool_name=prefix + tool, tool_input={"operations": []}, cwd=self.repo_dir)
                        )
                    )
        # Control: the same tool on the KB server, either spelling, is denied.
        for tool_name in (
            "mcp__plugin_gitian-kb_gitian-kb__append_entry",
            "mcp__gitian-kb__append_entry",
            "mcp__Gitian-KB__append_entry",  # the server segment is case-insensitive
        ):
            with self.subTest(tool_name=tool_name):
                self.assert_deny(self.run_hook(envelope(tool_name=tool_name, cwd=self.repo_dir)))


if __name__ == "__main__":
    unittest.main()
