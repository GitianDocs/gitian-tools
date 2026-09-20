#!/usr/bin/env python3
"""Unit tests for routing-guard.sh / routing_guard.py, the gitian-kb plugin's PreToolUse
routing-precondition guard.

Drives it end to end via `sh routing-guard.sh` (matching how hooks.json invokes it, on matcher
"mcp__.*(publish_doc|publish_entry|append_entry)"). The guard holds no state at all, so unlike the
nudge-layer tests there is no GITIAN_KB_STATE_FILE to isolate -- but the env is still copied and a
real throwaway git repo is created per test that needs one, so nothing reads the developer's own
checkout.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_routing_guard.py
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
ROUTING_GUARD_SH = HOOKS_DIR / "routing-guard.sh"

GITIAN_TOOL = "mcp__plugin_gitian-kb_gitian__append_entry"


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
        self.env = dict(os.environ)
        self.repos = []

    def tearDown(self):
        for path in self.repos:
            shutil.rmtree(path, ignore_errors=True)

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
            "mcp__plugin_gitian-kb_gitian__publish_doc",
            "mcp__plugin_gitian-kb_gitian__publish_entry",
            "mcp__plugin_gitian-kb_gitian__append_entry",
            "mcp__gitian__append_entry",  # the other server alias shape hooks already handle
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
            "mcp__plugin_gitian-kb_gitian__publish_memory",
            "mcp__plugin_gitian-kb_gitian__patch_doc",
            "mcp__plugin_gitian-kb_gitian__patch_memory",
            "mcp__plugin_gitian-kb_gitian__retract_item",
            "mcp__plugin_gitian-kb_gitian__publish_topic",
            "mcp__plugin_gitian-kb_gitian__retract_topic",
            "mcp__plugin_gitian-kb_gitian__search",
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


if __name__ == "__main__":
    unittest.main()
