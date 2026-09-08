#!/usr/bin/env python3
"""Unit tests for gitian-docs-nudge.sh, the gitian-docs plugin's PostToolUse hook.

Drives the hook end to end via `sh gitian-docs-nudge.sh` (matching how hooks.json actually
invokes it), against a throwaway git repo per test with CLAUDE_PROJECT_DIR pointed at it and
GITIAN_DOCS_STATE_DIR pointed at a fresh tempdir, so runs never touch a real
~/.claude/gitian-docs/ and never interfere with each other.

Every fixture project is a real git repo with its files STAGED (`git add -A`): `git grep`
searches tracked files only, so an unstaged fixture would make the annotations probe look broken
when it isn't. Every fixture also carries a README.md containing `@gitian:` -- a decoy proving
markdown is excluded from the probe, since prose that merely documents the syntax must not make a
repo look instrumented.

Runnable directly: python3 plugins/gitian-docs/hooks/tests/test_nudge.py
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
NUDGE_SH = HOOKS_DIR / "gitian-docs-nudge.sh"

# Global/system git config is neutralized for fixture setup so a developer's own excludesFile,
# templates, or hooks can never change what ends up tracked in a fixture repo.
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


class NudgeTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gitian-docs-nudge-test-")
        self.state_dir = os.path.join(self.tmpdir, "state")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def make_project(self, gitian_dir=False, annotations=False, name="proj"):
        """Build a git-initialized fixture project and return its path.

        gitian_dir  -- create `.gitian/config.yaml` (the strongest instrumentation signal)
        annotations -- create `src/a.ts` carrying an `@gitian:` tag
        The `@gitian:`-bearing README.md decoy is always present.
        """
        project = Path(self.tmpdir) / name
        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "README.md").write_text(
            "# fixture\n\nDocuments the `@gitian:todo` syntax but does not use it.\n",
            encoding="utf-8",
        )
        (project / "src" / "plain.js").write_text("export const x = 1;\n", encoding="utf-8")
        if gitian_dir:
            (project / ".gitian").mkdir(exist_ok=True)
            (project / ".gitian" / "config.yaml").write_text("tags: {}\n", encoding="utf-8")
        if annotations:
            (project / "src" / "a.ts").write_text(
                "// @gitian:todo x\nexport const a = 1;\n", encoding="utf-8"
            )
        subprocess.run(
            ["git", "init", "-q"], cwd=project, env=GIT_ENV, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "add", "-A"], cwd=project, env=GIT_ENV, check=True, capture_output=True
        )
        return project

    def run_hook(self, project, payload):
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(project)
        env["GITIAN_DOCS_STATE_DIR"] = self.state_dir
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(NUDGE_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    def edit(self, file_path, session_id="sess-1", tool_name="Edit"):
        return {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": {"file_path": file_path},
        }

    def bash(self, command, session_id="sess-1"):
        return {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }

    def assert_fires(self, proc, label=""):
        self.assertEqual(proc.returncode, 0, msg="%s stderr=%r" % (label, proc.stderr))
        payload = json.loads(proc.stdout)
        hso = payload["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PostToolUse")
        self.assertIn("gitian-docs skill", hso["additionalContext"])
        return hso["additionalContext"]

    def assert_silent(self, proc, label=""):
        self.assertEqual(proc.returncode, 0, msg="%s stderr=%r" % (label, proc.stderr))
        self.assertEqual(proc.stdout, "", msg="%s expected no output" % label)


class InstrumentationGate(NudgeTestCase):
    def test_uninstrumented_repo_stays_silent(self):
        project = self.make_project()
        self.assert_silent(self.run_hook(project, self.edit("src/a.ts")), "uninstrumented")

    def test_gitian_directory_fires(self):
        project = self.make_project(gitian_dir=True)
        self.assert_fires(self.run_hook(project, self.edit("src/plain.js")), "gitian-dir")

    def test_tracked_annotations_fire(self):
        project = self.make_project(annotations=True)
        self.assert_fires(self.run_hook(project, self.edit("src/a.ts")), "annotations")

    def test_markdown_decoy_alone_does_not_count_as_instrumented(self):
        # README.md mentions `@gitian:` in prose; nothing else does. Markdown is excluded from
        # the probe precisely so documenting the syntax can't fake instrumentation.
        project = self.make_project()
        self.assertIn("@gitian:", (project / "README.md").read_text(encoding="utf-8"))
        self.assert_silent(self.run_hook(project, self.edit("src/plain.js")), "md-decoy")


class OncePerSession(NudgeTestCase):
    def test_second_call_in_same_session_is_silent(self):
        project = self.make_project(gitian_dir=True)
        self.assert_fires(self.run_hook(project, self.edit("src/a.ts")), "first")
        self.assert_silent(self.run_hook(project, self.edit("src/plain.js")), "second")

    def test_a_different_session_id_fires_again(self):
        project = self.make_project(gitian_dir=True)
        self.assert_fires(self.run_hook(project, self.edit("src/a.ts")), "sess-1")
        self.assert_fires(
            self.run_hook(project, self.edit("src/a.ts", session_id="sess-2")), "sess-2"
        )

    def test_marker_older_than_seven_days_is_pruned(self):
        project = self.make_project(gitian_dir=True)
        self.assert_fires(self.run_hook(project, self.edit("src/a.ts")), "first")
        marker = os.path.join(self.state_dir, "gitian-docs-nudge-sess-1")
        self.assertTrue(os.path.isfile(marker))
        eight_days_ago = time.time() - 8 * 86400
        os.utime(marker, (eight_days_ago, eight_days_ago))
        self.assert_fires(self.run_hook(project, self.edit("src/a.ts")), "after-prune")


class ToolEligibility(NudgeTestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project(gitian_dir=True, annotations=True)

    def test_markdown_edit_is_silent(self):
        self.assert_silent(self.run_hook(self.project, self.edit("README.md")), "md-edit")

    def test_notebook_edit_fires(self):
        payload = {
            "session_id": "sess-nb",
            "tool_name": "NotebookEdit",
            "tool_input": {"notebook_path": "/repo/n.ipynb"},
        }
        self.assert_fires(self.run_hook(self.project, payload), "notebook")

    def test_non_editing_tool_is_silent(self):
        payload = {
            "session_id": "sess-read",
            "tool_name": "Read",
            "tool_input": {"file_path": "/repo/src/a.ts"},
        }
        self.assert_silent(self.run_hook(self.project, payload), "read")

    def test_bash_sed_in_place_on_a_code_file_fires(self):
        # The reason Bash is matched at all: in auto mode the model edits via sed/heredocs, which
        # an Edit|Write|MultiEdit matcher never sees.
        self.assert_fires(
            self.run_hook(self.project, self.bash("sed -i 's/a/b/' src/a.ts")), "sed -i"
        )

    def test_bash_read_only_command_is_silent(self):
        self.assert_silent(self.run_hook(self.project, self.bash("ls -la")), "ls")

    def test_bash_stderr_redirect_on_a_read_only_command_is_silent(self):
        # `2>/dev/null` is a read-only idiom, not a write; the `>` must not count as a signature.
        project = self.make_project(gitian_dir=True)
        self.assert_silent(
            self.run_hook(project, self.bash("grep -n x src/a.ts 2>/dev/null")), "stderr-redirect"
        )
        self.assert_silent(self.run_hook(project, self.bash("ls src/a.ts 2>&1")), "stderr-dup")

    def test_bash_heredoc_into_a_code_file_fires(self):
        project = self.make_project(gitian_dir=True)
        self.assert_fires(
            self.run_hook(project, self.bash("cat > src/b.ts <<'EOF'\nexport {};\nEOF")),
            "heredoc",
        )

    def test_bash_git_commit_fires(self):
        # A commit is when documentation gets reconciled, so it qualifies with no code token.
        self.assert_fires(self.run_hook(self.project, self.bash("git commit -m x")), "commit")

    def test_bash_redirect_into_markdown_is_silent(self):
        # A write signature without a code file is prose, not a documented-behavior change.
        self.assert_silent(self.run_hook(self.project, self.bash("echo hi > notes.md")), "md >")


class Robustness(NudgeTestCase):
    def test_garbage_stdin_is_silent_and_exits_zero(self):
        project = self.make_project(gitian_dir=True)
        proc = self.run_hook(project, "{not valid json ][ at all")
        self.assert_silent(proc, "garbage")

    def test_empty_stdin_is_silent_and_exits_zero(self):
        project = self.make_project(gitian_dir=True)
        self.assert_silent(self.run_hook(project, ""), "empty")


if __name__ == "__main__":
    unittest.main()
