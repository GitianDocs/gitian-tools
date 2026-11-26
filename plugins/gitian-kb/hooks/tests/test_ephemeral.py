#!/usr/bin/env python3
"""Unit tests for ephemeral.py, the one list of places where a write is not the user's work.

ephemeral is pure (it reads HOME, CLAUDE_CONFIG_DIR and TMPDIR and nothing else), so these call it
directly with all three pinned, and drive the CLI the way lib-state.sh's gks_is_ephemeral does.

The macOS cases are the reason the module exists: there `/tmp` and `/var` are links into
`/private`, every tool is handed the resolved spelling, and a session's scratchpad writes were
listed at its checkpoint as `/private (no GitHub remote): 34 file edits`.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_ephemeral.py
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOKS_DIR))

import ephemeral  # noqa: E402  (the module under test lives one directory up)

HOME = "/home/tester"


class EphemeralTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HOME": HOME})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        os.environ.pop("TMPDIR", None)

    def assert_ephemeral(self, *paths):
        for path in paths:
            with self.subTest(path=path):
                self.assertTrue(ephemeral.is_ephemeral(path))

    def assert_work(self, *paths):
        for path in paths:
            with self.subTest(path=path):
                self.assertFalse(ephemeral.is_ephemeral(path))


class ScratchSpace(EphemeralTestCase):
    def test_temp_directories_are_not_work(self):
        self.assert_ephemeral(
            "/tmp",
            "/tmp/x.txt",
            "/tmp/claude-1000/-home-me-repo/abc/scratchpad/notes.md",
            "/var/tmp/x.txt",
            "/dev/null",
            "/proc/self/oom_score_adj",
        )

    def test_the_macos_spellings_are_not_work_either(self):
        # /tmp -> /private/tmp and /var -> /private/var; $TMPDIR is /var/folders/<..>/T.
        self.assert_ephemeral(
            "/private/tmp/claude-501/-Users-me-repo/abc/scratchpad/notes.md",
            "/private/var/tmp/x.txt",
            "/var/folders/zz/abc123/T/draft.md",
            "/private/var/folders/zz/abc123/T/draft.md",
        )

    def test_a_name_that_only_starts_like_a_scratch_root_is_work(self):
        self.assert_work(
            "/tmpfile",
            "/tmp-backup/x",
            "/private",
            "/private/etc/hosts",
            "/var/lib/x",
            "/var/folder/x",
            "/devices/x",
            HOME + "/tmp/x",
            HOME + "/projects/repo/tmp/x.ts",
        )

    def test_dot_dot_and_doubled_slashes_are_folded_before_the_check(self):
        self.assert_ephemeral("/tmp/../tmp/x", "//tmp//x", "/tmp/./x")
        self.assert_work("/tmp/../repo/x", "/private/tmp/../../repo/x")

    def test_a_path_that_cannot_be_placed_is_counted(self):
        self.assert_work("", "relative/x.txt", "./tmp/x", "~/x", None, 7)


class TmpdirVariable(EphemeralTestCase):
    def test_tmpdir_is_scratch_space_wherever_it_points(self):
        with mock.patch.dict(os.environ, {"TMPDIR": "/run/user/1000/tmp/"}):
            self.assert_ephemeral("/run/user/1000/tmp/x", "/run/user/1000/tmp")
            self.assert_work("/run/user/1000/other/x", "/run/user/1000/tmpx")
        self.assert_work("/run/user/1000/tmp/x")  # unset again: an ordinary path

    def test_a_tmpdir_inside_home_is_still_scratch(self):
        with mock.patch.dict(os.environ, {"TMPDIR": HOME + "/.cache/tmp"}):
            self.assert_ephemeral(HOME + "/.cache/tmp/x")
            self.assert_work(HOME + "/.cache/other", HOME + "/projects/repo/a.ts")

    def test_a_tmpdir_that_would_swallow_real_work_is_ignored(self):
        for value in ("/", HOME, HOME + "/", "/home", "relative/tmp", ""):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TMPDIR": value}):
                self.assert_work(HOME + "/projects/repo/a.ts", "/srv/repo/a.ts")
                self.assert_ephemeral("/tmp/x")  # the fixed roots do not depend on it


class ClaudeCodeState(EphemeralTestCase):
    def test_projects_and_plans_under_the_default_config_dir_are_not_work(self):
        self.assert_ephemeral(
            HOME + "/.claude/projects/-work-repo/memory/note.md",
            HOME + "/.claude/plans/x.md",
        )

    def test_authored_source_under_the_config_dir_is_work(self):
        self.assert_work(
            HOME + "/.claude/plugins/cache/m/p/1.0.0/hooks/a.sh",
            HOME + "/.claude/agents/x.md",
            HOME + "/.claude/skills/s/SKILL.md",
            HOME + "/.claude/settings.json",
            HOME + "/.claude-backup/projects/a.md",
        )

    def test_the_config_dir_override_moves_the_exemption(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/opt/cfg/"}):
            self.assert_ephemeral("/opt/cfg/projects/p/memory/x.md", "/opt/cfg/plans/x.md")
            self.assert_work(HOME + "/.claude/plans/x.md", "/opt/cfg/skills/s/SKILL.md")


class CommandLine(EphemeralTestCase):
    def run_cli(self, *args, env=None):
        merged = dict(os.environ)
        merged.update(env or {})
        return subprocess.run(
            [sys.executable, str(HOOKS_DIR / "ephemeral.py"), *args],
            capture_output=True,
            text=True,
            env=merged,
            timeout=10,
        )

    def test_it_says_yes_for_an_ephemeral_path_and_nothing_otherwise(self):
        for args, expected in (
            (("/private/tmp/claude-501/x/scratchpad/a.md",), "yes\n"),
            ((HOME + "/.claude/plans/x.md",), "yes\n"),
            ((HOME + "/projects/repo/a.ts",), ""),
            (("relative.txt",), ""),
            ((), ""),
        ):
            with self.subTest(args=args):
                proc = self.run_cli(*args)
                self.assertEqual((proc.returncode, proc.stdout, proc.stderr), (0, expected, ""))

    def test_it_reads_tmpdir_from_its_environment(self):
        proc = self.run_cli("/run/user/1000/t/x", env={"TMPDIR": "/run/user/1000/t"})
        self.assertEqual((proc.returncode, proc.stdout), (0, "yes\n"))


if __name__ == "__main__":
    unittest.main()
