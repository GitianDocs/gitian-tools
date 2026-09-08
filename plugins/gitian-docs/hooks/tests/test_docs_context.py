#!/usr/bin/env python3
"""Unit tests for docs-context.sh, the gitian-docs plugin's SessionStart hook.

Drives the hook end to end via `sh docs-context.sh` (matching how hooks.json actually invokes
it), against a throwaway git repo per test with CLAUDE_PROJECT_DIR pointed at it and
GITIAN_DOCS_STATE_DIR pointed at a fresh tempdir, so runs never touch a real
~/.claude/gitian-docs/ and never interfere with each other.

Every fixture project is a real git repo with its files STAGED (`git add -A`): `git grep`
searches tracked files only, so an unstaged fixture would make the annotations probe look broken
when it isn't. Every fixture also carries a README.md containing `@gitian:` -- a decoy proving
markdown is excluded from the probe, since prose that merely documents the syntax must not make a
repo look instrumented.

Runnable directly: python3 plugins/gitian-docs/hooks/tests/test_docs_context.py
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
DOCS_CONTEXT_SH = HOOKS_DIR / "docs-context.sh"

# Global/system git config is neutralized for fixture setup so a developer's own excludesFile,
# templates, or hooks can never change what ends up tracked in a fixture repo.
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)


class DocsContextTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gitian-docs-context-test-")
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

    def run_hook(self, project, source="startup", session_id="sess-1", state_dir=None):
        env = dict(os.environ)
        env["CLAUDE_PROJECT_DIR"] = str(project)
        env["GITIAN_DOCS_STATE_DIR"] = state_dir or self.state_dir
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        payload = {
            "session_id": session_id,
            "transcript_path": "/dev/null",
            "cwd": str(project),
            "hook_event_name": "SessionStart",
            "source": source,
        }
        return subprocess.run(
            ["sh", str(DOCS_CONTEXT_SH)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )

    def context_of(self, proc, label=""):
        self.assertEqual(proc.returncode, 0, msg="%s stderr=%r" % (label, proc.stderr))
        try:
            payload = json.loads(proc.stdout)
        except Exception as exc:  # pragma: no cover - assertion path
            self.fail("stdout not valid JSON for %r: %r (%s)" % (label, proc.stdout, exc))
        hso = payload["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "SessionStart")
        return hso["additionalContext"]

    def assert_silent(self, proc, label=""):
        self.assertEqual(proc.returncode, 0, msg="%s stderr=%r" % (label, proc.stderr))
        self.assertEqual(proc.stdout, "", msg="%s expected no output" % label)


class Cue(DocsContextTestCase):
    def test_gitian_directory_emits_the_cue(self):
        project = self.make_project(gitian_dir=True)
        context = self.context_of(self.run_hook(project), "gitian-dir")
        # The Skill-tool id, not just the skill name: the cue exists because Claude Code can
        # elide a never-used skill's description from the listing, so the model has to be able
        # to invoke it straight from this line.
        self.assertIn("gitian-docs:gitian-docs", context)
        self.assertIn(".gitian/ present", context)
        self.assertIn("NEVER add gitian markup", context)

    def test_annotations_emit_the_cue_with_the_annotation_reason(self):
        project = self.make_project(annotations=True)
        context = self.context_of(self.run_hook(project), "annotations")
        self.assertIn("@gitian annotations present", context)
        self.assertIn("gitian-docs:gitian-docs", context)

    def test_uninstrumented_repo_says_nothing(self):
        project = self.make_project()
        self.assert_silent(self.run_hook(project), "uninstrumented")


class ClearRearmsTheNudge(DocsContextTestCase):
    def test_source_clear_deletes_the_nudge_marker(self):
        project = self.make_project(gitian_dir=True)
        os.makedirs(self.state_dir, exist_ok=True)
        marker = os.path.join(self.state_dir, "gitian-docs-nudge-sess-1")
        Path(marker).write_text("", encoding="utf-8")
        self.run_hook(project, source="clear")
        self.assertFalse(os.path.exists(marker), "clear must re-arm the once-per-session nudge")

    def test_source_clear_deletes_the_probe_cache_too(self):
        # A repo instrumented mid-session gets picked up at the next /clear, not never.
        project = self.make_project(gitian_dir=True)
        os.makedirs(self.state_dir, exist_ok=True)
        cache = os.path.join(self.state_dir, "probe-sess-1")
        Path(cache).write_text("\t%s\n" % project, encoding="utf-8")
        self.run_hook(project, source="clear")
        verdict = Path(cache).read_text(encoding="utf-8").split("\t")[0]
        self.assertEqual(verdict, "gitian-dir", "clear must drop the stale verdict and re-probe")

    def test_source_startup_leaves_the_nudge_marker_alone(self):
        project = self.make_project(gitian_dir=True)
        os.makedirs(self.state_dir, exist_ok=True)
        marker = os.path.join(self.state_dir, "gitian-docs-nudge-sess-1")
        Path(marker).write_text("", encoding="utf-8")
        self.run_hook(project, source="startup")
        self.assertTrue(os.path.exists(marker))


class ProbeCache(DocsContextTestCase):
    def cache_path(self, session_id="sess-1"):
        return os.path.join(self.state_dir, "probe-%s" % session_id)

    def test_cache_is_written_and_reused_for_the_same_project_dir(self):
        project = self.make_project(annotations=True)
        self.context_of(self.run_hook(project), "first")

        cache = self.cache_path()
        self.assertTrue(os.path.isfile(cache))
        verdict, cached_dir = Path(cache).read_text(encoding="utf-8").rstrip("\n").split("\t")
        self.assertEqual(verdict, "annotations")
        self.assertEqual(cached_dir, str(project))

        # Remove the only annotated file: a fresh probe would now find nothing, so a second run
        # that still reports "instrumented" can only have come from the cache.
        os.remove(project / "src" / "a.ts")
        context = self.context_of(self.run_hook(project), "cached")
        self.assertIn("@gitian annotations present", context)

        # Control: with a fresh state dir there is no cache, and the same project goes quiet.
        fresh = os.path.join(self.tmpdir, "state-fresh")
        self.assert_silent(self.run_hook(project, state_dir=fresh), "uncached-control")

    def test_cache_recorded_for_another_project_dir_is_recomputed(self):
        project = self.make_project()  # uninstrumented
        os.makedirs(self.state_dir, exist_ok=True)
        Path(self.cache_path()).write_text("gitian-dir\t/somewhere/else\n", encoding="utf-8")

        self.assert_silent(self.run_hook(project), "stale-cache")

        verdict, cached_dir = (
            Path(self.cache_path()).read_text(encoding="utf-8").rstrip("\n").split("\t")
        )
        self.assertEqual(verdict, "")
        self.assertEqual(cached_dir, str(project))


if __name__ == "__main__":
    unittest.main()
