#!/usr/bin/env python3
"""Unit tests for bash_activity.py, the PostToolUse hook that records what a Bash command did in the
gitian-kb nudge layer's activity ledger (and replaced the once-per-session commit nudge).

Drives it end to end via `sh bash-activity.sh` (matching how hooks.json actually invokes it), with
GITIAN_KB_STATE_FILE pointed at a fresh tempdir per test so runs never touch a real
~/.claude/gitian-kb/state.json. Commits are REAL ones, made in throwaway git repos whose `origin` is
git@github.com:acme/widgets.git (git runs with the global and system config switched off: this host
signs commits by default and the signer hangs). A stand-in `gh` is first on the hook's PATH and
fails, and any call to it fails the test: the hook only reads the command, and a merge is the Stop
poll's to find.

One kind of test needs care about WHERE its repo lives. bash_actions does not count a write into
scratch space (/tmp, /var/tmp, /dev, /proc) as work, so a checkout that is to be edited is made
under a fake $HOME created inside the REAL home directory; when that is not writable, the write
tests skip.

What is pinned here is what the HOOK writes, when it writes nothing at all, that it never prints,
and that it never holds the state lock across git. The decisions the ledger makes from what is
recorded are test_ledger.py's, the compliant-session gate is test_invariant.py's, and the command
parsing is test_bash_actions.py's.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_bash_activity.py
"""

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
BASH_ACTIVITY_SH = HOOKS_DIR / "bash-activity.sh"
BASH_ACTIVITY_PY = HOOKS_DIR / "bash_activity.py"

sys.path.insert(0, str(HOOKS_DIR))

import ledger as ledger_mod  # noqa: E402  (the module the hook records into lives one directory up)

SID = "sess-1"
REPO = "acme/widgets"
REMOTE = "git@github.com:acme/widgets.git"
TRANSCRIPT = "/tmp/transcript.jsonl"
SCRATCH_ROOTS = ("/tmp", "/var/tmp", "/dev", "/proc")

# `gh` is a test double that records the call and fails: nothing here may ever reach GitHub.
FAKE_GH = r"""#!/bin/sh
printf 'gh %s\n' "$*" >> "$GKS_PROBE_LOG"
exit 1
"""

# `git` that notes the call, reports whether the state lock was free while it ran, then runs the
# real one: how the tests see that git is never asked anything it need not be, and never under the
# lock.
PROBE_GIT = r"""#!/bin/sh
printf 'git %s\n' "$*" >> "$GKS_PROBE_LOG"
if [ -n "${GKS_PROBE_LOCK-}" ]; then
  python3 -c 'import fcntl, os, sys
os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)
fh = open(sys.argv[1], "a+")
try:
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    print("free")
except OSError:
    print("held")' "$GKS_PROBE_LOCK" >> "$GKS_PROBE_LOCKLOG"
fi
exec "$GKS_REAL_GIT" "$@"
"""

# Commands that do none of the things the ledger records. Several MENTION one: a quoted
# `git commit`, a heredoc documenting a workflow, a script called `commit`.
NOT_ACTIVITY = [
    "echo hello",
    "ls -la",
    "git status",
    "git diff --stat",
    "git log --grep commit",
    "git commit --dry-run -m x",
    "echo please git commit now",
    "echo 'git commit'",
    "echo 'a; git commit -m b; c'",
    "git add -A && echo next step is git commit",
    "npm run commit",
    "gh pr view 123",
    "gh pr list --state open",
    "gh release view v1.2.0",
    "gh release edit v1.2.0 --notes 'new notes'",
    "sed 's/a/b/' src/app.ts",
    "echo hello > /tmp/scratch.txt",
    "ls 2>/dev/null",
    "cat <<'EOF'\nrun git commit when you are done\nEOF",
    "cat <<'EOF'\ngh pr merge 12 --auto\nEOF",
]


def envelope(command, stdout="", stderr="", cwd="/work", session_id=SID, **extra):
    """A Bash PostToolUse envelope, in the shape Claude Code sends."""
    payload = {
        "session_id": session_id,
        "transcript_path": TRANSCRIPT,
        "cwd": cwd,
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout,
            "stderr": stderr,
            "interrupted": False,
            "isImage": False,
        },
    }
    payload.update(extra)
    return payload


def pr_url(number, repo=REPO):
    return "https://github.com/%s/pull/%d\n" % (repo, number)


def release_url(tag, repo=REPO):
    return "https://github.com/%s/releases/tag/%s\n" % (repo, tag)


def without(entries, *keys):
    return [{k: v for k, v in entry.items() if k not in keys} for entry in entries]


class BashActivityTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-bash-activity-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.bin_dir = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.bin_dir)
        self.probe_log = os.path.join(self.tmpdir, "probe.log")
        self.lock_log = os.path.join(self.tmpdir, "lock.log")
        self.real_git = shutil.which("git") or "git"
        self.home, self.counts_writes = self._make_home()
        self.addCleanup(shutil.rmtree, self.home, True)

        self.env = dict(os.environ)
        for name in ("GITIAN_KB_URL", "CLAUDE_CONFIG_DIR", "GH_REPO"):
            self.env.pop(name, None)
        self.env.update(
            {
                "GITIAN_KB_STATE_FILE": self.state_file,
                "HOME": self.home,
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Test Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Test Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.com",
                "GKS_PROBE_LOG": self.probe_log,
                "GKS_PROBE_LOCKLOG": self.lock_log,
                "GKS_REAL_GIT": self.real_git,
                "PATH": self.bin_dir + os.pathsep + os.environ.get("PATH", ""),
            }
        )
        self.install(FAKE_GH, "gh")

    def _make_home(self):
        """A fake $HOME for the hook, inside the real home so that a write under it is counted as
        work (see the module docstring); (path, whether writes there count)."""
        try:
            home = tempfile.mkdtemp(prefix=".gks-bash-activity-test-", dir=os.path.expanduser("~"))
        except OSError:
            home = tempfile.mkdtemp(prefix="gks-bash-activity-test-")
        scratch = any(home == root or home.startswith(root + "/") for root in SCRATCH_ROOTS)
        return home, not scratch

    def require_counted_writes(self):
        if not self.counts_writes:
            self.skipTest("the real home directory is not writable: a write under /tmp is scratch")

    def install(self, script, name):
        path = os.path.join(self.bin_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(script)
        os.chmod(path, 0o755)

    # --- running ---------------------------------------------------------------------------

    def run_hook(self, payload):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            ["sh", str(BASH_ACTIVITY_SH)],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=20,
        )

    def run_python(self, payload):
        """The module itself, without the wrapper: what it prints is what it prints."""
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [sys.executable, str(BASH_ACTIVITY_PY)],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=20,
        )

    # --- git -------------------------------------------------------------------------------

    def git(self, repo, *args):
        proc = subprocess.run(
            [self.real_git, "-C", repo, "-c", "commit.gpgsign=false"] + list(args),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg="git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout

    def make_repo(self, name="widgets", remote=REMOTE, branch="main", parent=None):
        path = os.path.join(parent or self.home, name)
        os.makedirs(path)
        self.git(path, "init", "-q", "-b", branch)
        if remote:
            self.git(path, "remote", "add", "origin", remote)
        return path

    def commit_in(self, repo, message="Fix the widget loader"):
        """Make a REAL commit; returns git's own output, which is what the hook is handed."""
        with open(os.path.join(repo, "a.txt"), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        self.git(repo, "add", "-A")
        return self.git(repo, "commit", "-m", message)

    def head(self, repo):
        return self.git(repo, "log", "-1", "--format=%h").strip()

    # --- state -----------------------------------------------------------------------------

    def state(self):
        with open(self.state_file, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def session(self, sid=SID):
        return self.state().get("sessions", {}).get(sid, {})

    def ledger(self, sid=SID):
        return self.session(sid).get("ledger", {})

    def events(self, sid=SID):
        return without(self.ledger(sid).get("events", []), "at")

    def buckets(self, sid=SID):
        return without(self.ledger(sid).get("edits", []), "min", "at")

    def edit_count(self, sid=SID):
        return sum(bucket["n"] for bucket in self.ledger(sid).get("edits", []))

    def watch(self, sid=SID):
        return without(self.session(sid).get("watch", []), "addedAt")

    def probes(self):
        if not os.path.exists(self.probe_log):
            return []
        with open(self.probe_log, "r", encoding="utf-8") as fh:
            return fh.read().splitlines()

    # --- assertions ------------------------------------------------------------------------

    def assert_silent(self, proc):
        """The hook has no output channel at all: exit 0, nothing on stdout, nothing on stderr."""
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def assert_no_state(self):
        """The state file was never read for writing: not even its directory exists."""
        self.assertFalse(os.path.exists(os.path.dirname(self.state_file)))

    def assert_no_gh(self):
        self.assertEqual([line for line in self.probes() if line.startswith("gh ")], [])

    def assert_just_now(self, stamp):
        parsed = ledger_mod.parse_iso(stamp)
        self.assertIsNotNone(parsed, msg="not a timestamp: %r" % (stamp,))
        age = (datetime.now(timezone.utc) - parsed).total_seconds()
        self.assertLess(abs(age), 120, msg="%s is not recent (%.0fs old)" % (stamp, age))


class Commits(BashActivityTestCase):
    def test_a_real_commit_records_its_sha_subject_and_repo(self):
        repo = self.make_repo()
        out = self.commit_in(repo, "Fix the widget loader")
        sha = self.head(repo)

        self.assert_silent(
            self.run_hook(envelope("git commit -m 'Fix the widget loader'", stdout=out, cwd=repo))
        )

        self.assertEqual(
            self.events(),
            [{"kind": "commit", "repo": REPO, "ref": sha, "text": "Fix the widget loader"}],
        )
        self.assert_just_now(self.ledger()["events"][0]["at"])
        self.assert_no_gh()

    def test_a_message_written_as_a_heredoc_substitution_is_still_a_commit(self):
        # The form a multi-line message nearly always takes -- and the one the old regex split cut
        # in half, so the commit it made was never seen. The message even mentions `git commit`.
        repo = self.make_repo()
        with open(os.path.join(repo, "a.txt"), "w", encoding="utf-8") as fh:
            fh.write("loader\n")
        command = (
            "git add -A && git commit -m \"$(cat <<'EOF'\n"
            'Add the loader\n\nRun git commit; and && | on purpose.\nEOF\n)"'
        )
        real = subprocess.run(
            ["sh", "-c", command],
            cwd=repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(real.returncode, 0, msg=real.stderr)
        sha = self.head(repo)

        self.assert_silent(self.run_hook(envelope(command, stdout=real.stdout, cwd=repo)))

        self.assertEqual(
            self.events(), [{"kind": "commit", "repo": REPO, "ref": sha, "text": "Add the loader"}]
        )

    def test_every_way_of_naming_the_commit_is_found(self):
        repo = self.make_repo()
        self.commit_in(repo, "Fix the widget loader")
        sha = self.head(repo)
        parent = os.path.dirname(repo)
        shapes = {
            "plain": ("git commit -m 'fix things'", repo),
            "dash-C relative": ("git -C widgets commit -m x", parent),
            "dash-C absolute": ("git -C %s commit -m x" % repo, "/"),
            "dash-c option": ("git -c user.name=x commit -m x", repo),
            "cd first": ("cd %s && git commit -m x" % repo, "/"),
            "newline separated": ("git add -A\ngit commit -m x", repo),
            "chained": ("git add -A && git commit -m x", repo),
            "env prefix": ("FOO=bar git commit -m x", repo),
            "absolute git": ("/usr/bin/git commit -m x", repo),
            "heredoc message": ("git commit -m \"$(cat <<'EOF'\nsubject\n\nbody\nEOF\n)\"", repo),
        }
        for label, (command, cwd) in shapes.items():
            with self.subTest(shape=label):
                self.assert_silent(self.run_hook(envelope(command, cwd=cwd, session_id=label)))
                self.assertEqual(
                    self.events(label),
                    [{"kind": "commit", "repo": REPO, "ref": sha, "text": "Fix the widget loader"}],
                )

    def test_the_commit_belongs_to_the_directory_it_ran_in_not_the_session_cwd(self):
        widgets = self.make_repo()
        gadgets = self.make_repo(name="gadgets", remote="https://github.com/acme/gadgets.git")
        self.commit_in(widgets, "Widgets only")
        self.commit_in(gadgets, "Gadgets only")

        self.assert_silent(self.run_hook(envelope("git -C ../gadgets commit -m x", cwd=widgets)))

        self.assertEqual(
            self.events(),
            [
                {
                    "kind": "commit",
                    "repo": "acme/gadgets",
                    "ref": self.head(gadgets),
                    "text": "Gadgets only",
                }
            ],
        )

    def test_a_checkout_without_a_remote_is_labelled_by_place(self):
        repo = self.make_repo(name="plain", remote=None)
        out = self.commit_in(repo, "Local only")

        self.assert_silent(self.run_hook(envelope("git commit -m x", stdout=out, cwd=repo)))

        # The hook's $HOME is the fake one, so the label is stable: `~/` and the checkout's name.
        self.assertEqual(
            self.events(),
            [{"kind": "commit", "where": "~/plain", "ref": self.head(repo), "text": "Local only"}],
        )

    def test_the_same_commit_twice_is_one_event_and_the_next_commit_is_another(self):
        repo = self.make_repo()
        self.commit_in(repo, "First")
        command = "git commit -m First"
        self.assert_silent(self.run_hook(envelope(command, cwd=repo)))
        self.assert_silent(self.run_hook(envelope(command, cwd=repo)))
        self.assertEqual(len(self.events()), 1)

        self.commit_in(repo, "Second")
        self.assert_silent(self.run_hook(envelope("git commit -m Second", cwd=repo)))
        self.assertEqual([e["text"] for e in self.events()], ["First", "Second"])

    def test_a_command_that_commits_twice_in_one_directory_asks_git_once(self):
        repo = self.make_repo()
        self.commit_in(repo)
        self.install(PROBE_GIT, "git")

        self.assert_silent(self.run_hook(envelope("git commit -m a; git commit -m b", cwd=repo)))

        self.assertEqual(len(self.events()), 1)
        asked = [line for line in self.probes() if " log " in line]
        self.assertEqual(len(asked), 1)

    def test_a_failed_commit_records_nothing_and_touches_no_state(self):
        repo = self.make_repo()
        self.commit_in(repo)
        self.install(PROBE_GIT, "git")
        failures = {
            "nothing to commit": ("nothing to commit, working tree clean\n", ""),
            "error marker": ("", "error: pathspec 'x' did not match any file(s) known to git"),
            "fatal marker": ("", "fatal: not a git repository (or any of the parent directories)"),
            "aborting, capitalized": ("", "Aborting commit due to empty commit message.\n"),
        }
        for label, (stdout, stderr) in failures.items():
            with self.subTest(failure=label):
                self.assert_silent(
                    self.run_hook(
                        envelope("git commit -m noop", stdout=stdout, stderr=stderr, cwd=repo)
                    )
                )
        self.assert_no_state()
        self.assertEqual(self.probes(), [])  # not even a question to git

    def test_a_failed_commit_does_not_stop_a_real_one_being_recorded_later(self):
        repo = self.make_repo()
        out = self.commit_in(repo)
        self.run_hook(envelope("git commit -m noop", stderr="error: nope", cwd=repo))
        self.assert_silent(self.run_hook(envelope("git commit -m real", stdout=out, cwd=repo)))
        self.assertEqual(len(self.events()), 1)

    def test_the_response_is_read_in_whatever_shape_it_arrives(self):
        repo = self.make_repo()
        self.commit_in(repo)
        for label, response, recorded in (
            ("string, failed", "nothing to commit, working tree clean", False),
            ("string, fine", "[main abc1234] fix", True),
            ("bare list, failed", [{"type": "text", "text": "fatal: nope"}], False),
            ("no response", None, True),
        ):
            with self.subTest(response=label):
                payload = envelope("git commit -m x", cwd=repo, session_id=label)
                payload["tool_response"] = response
                self.assert_silent(self.run_hook(payload))
                if recorded:
                    self.assertEqual(len(self.events(label)), 1)
                elif os.path.exists(self.state_file):
                    self.assertEqual(self.events(label), [])


class PullRequests(BashActivityTestCase):
    def test_a_create_whose_output_names_the_pull_request_watches_it_and_records_it(self):
        repo = self.make_repo(branch="feature/loader")
        self.assert_silent(
            self.run_hook(
                envelope(
                    "gh pr create --title 'Add the loader' --body 'Details' --base main",
                    stdout=pr_url(12),
                    stderr="\nCreating pull request for feature/loader into main in acme/widgets\n",
                    cwd=repo,
                )
            )
        )
        self.assertEqual(self.watch(), [{"repo": REPO, "number": 12, "why": "opened"}])
        self.assertEqual(
            self.events(), [{"kind": "pr", "repo": REPO, "ref": "#12", "text": "Add the loader"}]
        )
        self.assert_no_gh()

    def test_the_repo_in_the_url_wins_over_a_forks_origin(self):
        repo = self.make_repo(remote="git@github.com:me/widgets.git")
        self.assert_silent(
            self.run_hook(envelope("gh pr create --fill", stdout=pr_url(7), cwd=repo))
        )
        self.assertEqual(self.watch(), [{"repo": REPO, "number": 7, "why": "opened"}])
        self.assertEqual(self.events(), [{"kind": "pr", "repo": REPO, "ref": "#7"}])

    def test_a_named_repo_needs_no_question_to_git(self):
        self.install(PROBE_GIT, "git")
        self.assert_silent(
            self.run_hook(
                envelope(
                    "gh pr create -R acme/gadgets --title Add",
                    stdout=pr_url(3, "acme/gadgets"),
                    cwd=self.home,
                )
            )
        )
        self.assertEqual(self.watch(), [{"repo": "acme/gadgets", "number": 3, "why": "opened"}])
        self.assertEqual(self.probes(), [])

    def test_a_title_the_command_computed_is_not_quoted_back(self):
        repo = self.make_repo()
        for label, command in (
            ("variable", 'gh pr create --title "$TITLE"'),
            ("substitution", 'gh pr create -t "$(cat title.txt)"'),
        ):
            with self.subTest(title=label):
                self.assert_silent(
                    self.run_hook(envelope(command, stdout=pr_url(9), cwd=repo, session_id=label))
                )
                self.assertEqual(self.events(label), [{"kind": "pr", "repo": REPO, "ref": "#9"}])

    def test_a_create_without_a_url_watches_the_current_branch_and_records_no_event(self):
        repo = self.make_repo(branch="feature/loader")
        self.assert_silent(self.run_hook(envelope("gh pr create --fill", cwd=repo)))
        self.assertEqual(
            self.watch(), [{"repo": REPO, "branch": "feature/loader", "why": "opened"}]
        )
        self.assertEqual(self.events(), [])

    def test_a_create_with_a_head_watches_that_branch(self):
        repo = self.make_repo(branch="feature/loader")
        self.assert_silent(
            self.run_hook(envelope("gh pr create --head topic/other --fill", cwd=repo))
        )
        self.assertEqual(self.watch(), [{"repo": REPO, "branch": "topic/other", "why": "opened"}])

    def test_the_same_pull_request_is_watched_and_recorded_once(self):
        repo = self.make_repo()
        for _ in range(2):
            self.assert_silent(
                self.run_hook(envelope("gh pr create --title T", stdout=pr_url(12), cwd=repo))
            )
        self.assertEqual(len(self.watch()), 1)
        self.assertEqual(len(self.events()), 1)

    def test_a_create_in_a_directory_with_nothing_to_watch_writes_nothing(self):
        # No URL, no remote: there is no repo to put a watch under, so no state is touched.
        plain = self.make_repo(name="plain", remote=None)
        self.assert_silent(self.run_hook(envelope("gh pr create --fill", cwd=plain)))
        self.assert_no_state()

    def test_an_auto_merge_is_watched_as_armed(self):
        repo = self.make_repo()
        self.assert_silent(
            self.run_hook(envelope("gh pr merge 12 --auto --squash", stdout="", cwd=repo))
        )
        self.assertEqual(self.watch(), [{"repo": REPO, "number": 12, "why": "armed"}])

    def test_a_plain_merge_is_watched_as_merge(self):
        repo = self.make_repo()
        self.assert_silent(
            self.run_hook(envelope("gh pr merge 12 --squash --delete-branch", cwd=repo))
        )
        self.assertEqual(self.watch(), [{"repo": REPO, "number": 12, "why": "merge"}])

    def test_a_bare_merge_watches_the_current_branch_armed_or_not(self):
        repo = self.make_repo(branch="feature/loader")
        self.assert_silent(
            self.run_hook(envelope("gh pr merge --auto", cwd=repo, session_id="armed"))
        )
        self.assertEqual(
            self.watch("armed"), [{"repo": REPO, "branch": "feature/loader", "why": "armed"}]
        )
        self.assert_silent(
            self.run_hook(envelope("gh pr merge --squash", cwd=repo, session_id="plain"))
        )
        self.assertEqual(
            self.watch("plain"), [{"repo": REPO, "branch": "feature/loader", "why": "merge"}]
        )

    def test_a_merge_names_its_pull_request_in_whatever_way_the_command_does(self):
        repo = self.make_repo(branch="feature/loader")
        forms = {
            "branch name": (
                "gh pr merge topic/other --squash",
                {"repo": REPO, "branch": "topic/other", "why": "merge"},
            ),
            "hash number": (
                "gh pr merge '#14' --squash",
                {"repo": REPO, "number": 14, "why": "merge"},
            ),
            "url": (
                "gh pr merge https://github.com/acme/gadgets/pull/9 --auto",
                {"repo": "acme/gadgets", "number": 9, "why": "armed"},
            ),
            "--repo": (
                "gh pr merge 5 --repo acme/gadgets --squash",
                {"repo": "acme/gadgets", "number": 5, "why": "merge"},
            ),
            "GH_REPO": (
                "GH_REPO=acme/gadgets gh pr merge 6 --auto",
                {"repo": "acme/gadgets", "number": 6, "why": "armed"},
            ),
        }
        for label, (command, expected) in forms.items():
            with self.subTest(form=label):
                self.assert_silent(self.run_hook(envelope(command, cwd=repo, session_id=label)))
                self.assertEqual(self.watch(label), [expected])

    def test_a_merge_is_never_an_event_whatever_the_command_printed(self):
        # `gh pr merge --auto` only ARMS a merge; a plain one is confirmed by the Stop poll like
        # any other. Merges come from the poll and from nowhere else.
        repo = self.make_repo()
        for label, command, stdout in (
            (
                "armed",
                "gh pr merge 12 --auto --squash",
                "Pull request #12 will be automatically merged via squash when checks pass\n",
            ),
            (
                "merged",
                "gh pr merge 12 --squash",
                "✓ Squashed and merged pull request #12 (Add the loader)\n",
            ),
        ):
            with self.subTest(merge=label):
                self.assert_silent(
                    self.run_hook(envelope(command, stdout=stdout, cwd=repo, session_id=label))
                )
                self.assertEqual(len(self.watch(label)), 1)
                self.assertEqual(self.events(label), [])

    def test_merging_the_same_pull_request_twice_watches_it_once(self):
        repo = self.make_repo()
        for _ in range(2):
            self.assert_silent(self.run_hook(envelope("gh pr merge 12 --auto", cwd=repo)))
        self.assertEqual(len(self.watch()), 1)

    def test_a_bare_merge_with_no_branch_to_name_writes_nothing(self):
        # A detached HEAD: `git branch --show-current` prints nothing, so there is nothing to watch.
        repo = self.make_repo()
        self.commit_in(repo)
        self.git(repo, "checkout", "-q", "--detach")
        self.assert_silent(self.run_hook(envelope("gh pr merge --auto", cwd=repo)))
        self.assert_no_state()

    def test_the_hook_never_calls_gh(self):
        repo = self.make_repo(branch="feature/loader")
        for command, stdout in (
            ("gh pr create --fill", pr_url(12)),
            ("gh pr create --fill", ""),
            ("gh pr merge 12 --auto", ""),
            ("gh pr merge --squash", ""),
            ("gh release create v1.0.0", release_url("v1.0.0")),
        ):
            self.assert_silent(self.run_hook(envelope(command, stdout=stdout, cwd=repo)))
        self.assert_no_gh()


class Releases(BashActivityTestCase):
    def test_a_release_create_records_its_tag_and_repo(self):
        repo = self.make_repo()
        self.assert_silent(
            self.run_hook(
                envelope(
                    "gh release create v1.2.0 --title v1.2.0 --notes 'Ship it'",
                    stdout=release_url("v1.2.0"),
                    cwd=repo,
                )
            )
        )
        self.assertEqual(self.events(), [{"kind": "release", "repo": REPO, "ref": "v1.2.0"}])
        self.assertEqual(self.watch(), [])
        self.assert_no_gh()

    def test_a_draft_create_is_flagged_and_keeps_the_tag_the_command_named(self):
        repo = self.make_repo()
        self.assert_silent(
            self.run_hook(
                envelope(
                    "gh release create v1.2.0 --draft --generate-notes",
                    stdout=release_url("untagged-1a2b3c4d"),
                    cwd=repo,
                )
            )
        )
        self.assertEqual(
            self.events(), [{"kind": "release", "repo": REPO, "ref": "v1.2.0", "draft": True}]
        )

    def test_publishing_a_draft_is_a_release(self):
        repo = self.make_repo()
        self.assert_silent(
            self.run_hook(
                envelope(
                    "gh release edit v1.2.0 --draft=false", stdout=release_url("v1.2.0"), cwd=repo
                )
            )
        )
        self.assertEqual(self.events(), [{"kind": "release", "repo": REPO, "ref": "v1.2.0"}])

    def test_any_other_edit_of_a_release_is_nothing(self):
        repo = self.make_repo()
        for command in ("gh release edit v1.2.0 --notes x", "gh release edit v1.2.0 --draft"):
            self.assert_silent(self.run_hook(envelope(command, cwd=repo)))
        self.assert_no_state()

    def test_the_url_supplies_what_the_command_could_not(self):
        # A tag in a variable and a directory that is no checkout: only the output knows.
        self.assert_silent(
            self.run_hook(
                envelope(
                    'gh release create "$TAG" --notes x',
                    stdout=release_url("v3.1.0"),
                    cwd=self.home,
                )
            )
        )
        self.assertEqual(self.events(), [{"kind": "release", "repo": REPO, "ref": "v3.1.0"}])

    def test_a_named_repo_needs_no_url_and_no_question_to_git(self):
        self.install(PROBE_GIT, "git")
        self.assert_silent(
            self.run_hook(
                envelope("gh release create v2.0.0 -R acme/gadgets --notes x", cwd=self.home)
            )
        )
        self.assertEqual(
            self.events(), [{"kind": "release", "repo": "acme/gadgets", "ref": "v2.0.0"}]
        )
        self.assertEqual(self.probes(), [])

    def test_a_release_from_a_checkout_without_a_remote_is_labelled_by_place(self):
        plain = self.make_repo(name="plain", remote=None)
        self.assert_silent(self.run_hook(envelope("gh release create v1", cwd=plain)))
        self.assertEqual(self.events(), [{"kind": "release", "where": "~/plain", "ref": "v1"}])

    def test_a_failed_release_records_nothing(self):
        repo = self.make_repo()
        failures = {
            "http error": "HTTP 422: Validation Failed (https://api.github.com/repos/a/b/releases)",
            "duplicate tag": "a release with the same tag name already exists: v1.2.0",
            "missing": "release not found",
            "error marker": "error: could not read notes file",
            "graphql": "GraphQL: Resource not accessible by integration (createRelease)",
        }
        for label, stderr in failures.items():
            with self.subTest(failure=label):
                self.assert_silent(
                    self.run_hook(envelope("gh release create v1.2.0", stderr=stderr, cwd=repo))
                )
        self.assert_no_state()


class Writes(BashActivityTestCase):
    def setUp(self):
        super().setUp()
        self.require_counted_writes()

    def test_sed_in_place_on_a_repo_file_is_an_edit_in_that_repos_bucket(self):
        repo = self.make_repo()
        self.assert_silent(self.run_hook(envelope("sed -i 's/a/b/' src/app.ts", cwd=repo)))
        self.assertEqual(self.buckets(), [{"repo": REPO, "n": 1}])
        self.assert_just_now(self.ledger()["edits"][0]["at"])

    def test_every_way_of_editing_a_file_from_the_shell_counts(self):
        repo = self.make_repo()
        shapes = {
            "sed -i": "sed -i 's/a/b/' src/app.ts",
            "sed -i.bak": "sed -i.bak 's/a/b/' src/app.ts",
            "perl -pi": "perl -pi -e 's/a/b/' src/app.pl",
            "redirect": "echo hi > notes.md",
            "append": "echo hi >> notes.md",
            "tee": "echo hi | tee notes.md",
            "git apply": "git apply fix.patch",
            "patch": "patch -p1 < fix.patch",
            "after cd": "cd src && sed -i 's/a/b/' app.ts",
        }
        for label, command in shapes.items():
            with self.subTest(shape=label):
                self.assert_silent(self.run_hook(envelope(command, cwd=repo, session_id=label)))
                self.assertEqual(self.edit_count(label), 1)
                self.assertEqual({b.get("repo") for b in self.ledger(label)["edits"]}, {REPO})

    def test_a_command_with_two_writes_is_two_edits_and_one_with_two_targets_is_one(self):
        repo = self.make_repo()
        self.assert_silent(
            self.run_hook(
                envelope(
                    "sed -i 's/a/b/' one.ts && sed -i 's/c/d/' two.ts", cwd=repo, session_id="two"
                )
            )
        )
        self.assertEqual(self.edit_count("two"), 2)
        self.assert_silent(
            self.run_hook(envelope("echo x | tee one.txt two.txt", cwd=repo, session_id="tee"))
        )
        self.assertEqual(self.edit_count("tee"), 1)

    def test_edits_in_two_checkouts_are_kept_apart(self):
        widgets = self.make_repo()
        gadgets = self.make_repo(name="gadgets", remote="https://github.com/acme/gadgets.git")
        self.assert_silent(self.run_hook(envelope("sed -i 's/a/b/' a.ts", cwd=widgets)))
        self.assert_silent(self.run_hook(envelope("sed -i 's/a/b/' ../gadgets/b.ts", cwd=widgets)))
        self.assert_silent(self.run_hook(envelope("echo x > b.ts", cwd=gadgets)))
        counts = {}
        for bucket in self.ledger()["edits"]:
            counts[bucket["repo"]] = counts.get(bucket["repo"], 0) + bucket["n"]
        self.assertEqual(counts, {REPO: 1, "acme/gadgets": 2})

    def test_a_checkout_without_a_remote_is_labelled_by_place(self):
        plain = self.make_repo(name="plain", remote=None)
        self.assert_silent(self.run_hook(envelope("echo x > notes.md", cwd=plain)))
        self.assertEqual(self.buckets(), [{"where": "~/plain", "n": 1}])

    def test_scratch_and_claude_codes_own_state_are_not_work(self):
        for command in (
            "echo x > /tmp/scratch.txt",
            "echo x | tee /var/tmp/scratch.txt",
            "echo x > ~/.claude/projects/p/memory/note.md",
            "echo x >> %s/.claude/plans/plan.md" % self.home,
            "sed -i 's/a/b/' /tmp/copy.txt",
        ):
            with self.subTest(command=command):
                self.assert_silent(self.run_hook(envelope(command, cwd=self.home)))
        self.assert_no_state()

    def test_authored_source_under_the_config_dir_is_work(self):
        # Only <config>/projects and <config>/plans are exempt: skills, agents and plugins are
        # source, and a session whose job is editing one must count.
        self.assert_silent(
            self.run_hook(envelope("echo x > ~/.claude/skills/s/SKILL.md", cwd=self.home))
        )
        self.assertEqual(self.edit_count(), 1)

    def test_a_target_the_command_computed_is_not_guessed_at(self):
        self.assert_silent(self.run_hook(envelope('echo x > "$OUT"', cwd=self.home)))
        self.assert_silent(self.run_hook(envelope("echo x | tee $(mktemp)", cwd=self.home)))
        self.assert_no_state()

    def test_a_command_that_only_reads_is_not_an_edit(self):
        repo = self.make_repo()
        for command in (
            "sed 's/a/b/' src/app.ts",
            "git apply --check fix.patch",
            "patch --dry-run < fix.patch",
        ):
            self.assert_silent(self.run_hook(envelope(command, cwd=repo)))
        self.assert_no_state()


class NothingToRecord(BashActivityTestCase):
    def test_a_command_that_did_none_of_it_leaves_no_trace_and_asks_nothing(self):
        # The hook runs on EVERY Bash call: for nearly all of them it must be a parse and an exit,
        # before the state directory exists and before a single git or gh process is started.
        self.install(PROBE_GIT, "git")
        for command in NOT_ACTIVITY:
            with self.subTest(command=command):
                self.assert_silent(self.run_hook(envelope(command, cwd=self.home)))
        self.assert_no_state()
        self.assertEqual(self.probes(), [])

    def test_a_quoted_mention_of_a_commit_in_a_pull_request_body_is_not_a_commit(self):
        # It IS a `gh pr create`, so its branch is watched -- but the words "git commit" in --body
        # are an argument, and no commit event is recorded.
        repo = self.make_repo(branch="feature/loader")
        self.assert_silent(
            self.run_hook(envelope('gh pr create --body "run git commit" --fill', cwd=repo))
        )
        self.assertEqual(self.events(), [])
        self.assertEqual(
            self.watch(), [{"repo": REPO, "branch": "feature/loader", "why": "opened"}]
        )

    def test_only_the_bash_tool_counts(self):
        for tool_name in ("Edit", "Write", "bash", "BashOutput", "Task", None, 7):
            with self.subTest(tool_name=tool_name):
                payload = envelope("git commit -m x")
                payload["tool_name"] = tool_name
                self.assert_silent(self.run_hook(payload))
        self.assert_no_state()

    def test_a_missing_or_odd_command_is_nothing(self):
        for command in (None, "", "   ", 42, ["git", "commit"], {"cmd": "git commit"}):
            with self.subTest(command=command):
                self.assert_silent(self.run_hook(envelope(command)))
        for tool_input in (None, "git commit -m x", [], {}):
            with self.subTest(tool_input=tool_input):
                payload = envelope("git commit -m x")
                payload["tool_input"] = tool_input
                self.assert_silent(self.run_hook(payload))
        self.assert_no_state()


class Subagents(BashActivityTestCase):
    """A subagent's hooks fire under the parent's session id, and its work is the session's work."""

    def test_a_subagents_calls_are_recorded_exactly_like_the_primarys(self):
        repo = self.make_repo(branch="feature/loader")
        out = self.commit_in(repo)
        calls = [
            ("git commit -m x", out),
            ("gh pr create --title 'Add the loader'", pr_url(12)),
            ("gh pr merge 12 --auto", ""),
            ("gh release create v1.0.0", release_url("v1.0.0")),
        ]
        if self.counts_writes:
            calls.append(("sed -i 's/a/b/' src/app.ts", ""))
        subagent = {"agent_id": "agent-123", "agent_type": "general-purpose"}
        for sid, extra in (("primary", {}), ("subagent", subagent)):
            for command, stdout in calls:
                self.assert_silent(
                    self.run_hook(
                        envelope(command, stdout=stdout, cwd=repo, session_id=sid, **extra)
                    )
                )

        self.assertEqual(len(self.events("primary")), 3)
        self.assertEqual(self.events("subagent"), self.events("primary"))
        self.assertEqual(self.watch("subagent"), self.watch("primary"))
        self.assertEqual(self.buckets("subagent"), self.buckets("primary"))

    def test_the_primarys_transcript_is_remembered_a_subagents_is_not(self):
        repo = self.make_repo()
        out = self.commit_in(repo)
        subagent = envelope(
            "git commit -m x", stdout=out, cwd=repo, session_id="a", agent_id="agent-1"
        )
        subagent["transcript_path"] = "/tmp/sidechain.jsonl"
        self.assert_silent(self.run_hook(subagent))
        self.assertEqual(len(self.events("a")), 1)
        self.assertNotIn("transcript", self.ledger("a"))

        self.assert_silent(
            self.run_hook(envelope("git commit -m x", stdout=out, cwd=repo, session_id="b"))
        )
        self.assertEqual(self.ledger("b")["transcript"], TRANSCRIPT)


class Silence(BashActivityTestCase):
    """The hook is RECORD-ONLY. Nothing it sees makes it print, however it is run."""

    def test_the_module_itself_prints_nothing_for_anything(self):
        repo = self.make_repo()
        out = self.commit_in(repo)
        payloads = [
            envelope("git commit -m x", stdout=out, cwd=repo),
            envelope("gh pr create --fill", stdout=pr_url(12), cwd=repo),
            envelope("gh pr merge 12 --auto", cwd=repo),
            envelope("gh release create v1", stdout=release_url("v1"), cwd=repo),
            envelope("git commit -m x", stderr="fatal: nope", cwd=repo),
            envelope("echo hello", cwd=repo),
            "{not valid json ][ at all",
            "",
            "[1, 2, 3]",
        ]
        for payload in payloads:
            with self.subTest(payload=str(payload)[:60]):
                proc = self.run_python(payload)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(proc.stderr, "")

    def test_the_source_has_no_way_to_print(self):
        tree = ast.parse(BASH_ACTIVITY_PY.read_text(encoding="utf-8"))
        imported = set()
        printing = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                printing.append(node.lineno)
            elif isinstance(node, ast.Attribute) and node.attr in ("stdout", "stderr"):
                printing.append(node.lineno)
        self.assertEqual(printing, [])
        self.assertEqual(imported, {"json", "sys", "bash_actions", "ledger"})


class Robustness(BashActivityTestCase):
    def test_empty_garbage_and_non_object_stdin_are_silent(self):
        for payload in ("", "{not valid json ][ at all", "[1, 2, 3]", "null", '"git commit"'):
            with self.subTest(stdin=payload):
                self.assert_silent(self.run_hook(payload))
        self.assert_no_state()

    def test_without_a_session_id_nothing_is_recorded(self):
        repo = self.make_repo()
        for session_id in (None, "", 7, ["sess-1"]):
            with self.subTest(session_id=session_id):
                payload = envelope("git commit -m x", cwd=repo)
                payload["session_id"] = session_id
                self.assert_silent(self.run_hook(payload))
        payload = envelope("git commit -m x", cwd=repo)
        del payload["session_id"]
        self.assert_silent(self.run_hook(payload))
        self.assert_no_state()

    def test_a_missing_cwd_does_not_stop_a_named_repo_being_recorded(self):
        payload = envelope("gh release create v1 -R acme/gadgets")
        del payload["cwd"]
        self.assert_silent(self.run_hook(payload))
        self.assertEqual(self.events(), [{"kind": "release", "repo": "acme/gadgets", "ref": "v1"}])

    def test_a_corrupt_state_file_is_rebuilt_and_the_commit_recorded(self):
        repo = self.make_repo()
        out = self.commit_in(repo)
        os.makedirs(os.path.dirname(self.state_file))
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")

        self.assert_silent(self.run_hook(envelope("git commit -m x", stdout=out, cwd=repo)))

        self.assertEqual(self.state()["schemaVersion"], 1)
        self.assertEqual(len(self.events()), 1)

    def test_a_corrupt_state_file_is_left_alone_by_a_command_that_records_nothing(self):
        os.makedirs(os.path.dirname(self.state_file))
        with open(self.state_file, "w", encoding="utf-8") as fh:
            fh.write("{not valid json ][ at all")
        self.assert_silent(self.run_hook(envelope("git log --grep commit")))
        self.assert_silent(self.run_hook(envelope("git commit -m x", stderr="fatal: nope")))
        with open(self.state_file, "r", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{not valid json ][ at all")

    def test_a_corrupt_session_record_is_rebuilt(self):
        repo = self.make_repo()
        out = self.commit_in(repo)
        os.makedirs(os.path.dirname(self.state_file))
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump({"schemaVersion": 1, "servers": {}, "sessions": {SID: "garbage"}}, fh)

        self.assert_silent(self.run_hook(envelope("git commit -m x", stdout=out, cwd=repo)))

        self.assertEqual(len(self.events()), 1)

    def test_an_unusable_state_location_is_silent(self):
        repo = self.make_repo()
        out = self.commit_in(repo)
        # A directory where the state file should be.
        os.makedirs(self.state_file)
        self.assert_silent(self.run_hook(envelope("git commit -m x", stdout=out, cwd=repo)))
        # A regular file where the state directory should be.
        blocker = os.path.join(self.tmpdir, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("not a directory")
        self.env["GITIAN_KB_STATE_FILE"] = os.path.join(blocker, "nested", "state.json")
        self.assert_silent(self.run_hook(envelope("git commit -m x", stdout=out, cwd=repo)))

    def test_git_is_asked_before_the_state_lock_is_taken(self):
        # The lock serializes every hook of every session on the machine; git has a 2 s cap.
        repo = self.make_repo(branch="feature/loader")
        out = self.commit_in(repo)
        self.install(PROBE_GIT, "git")
        self.env["GKS_PROBE_LOCK"] = self.state_file + ".lock"

        self.assert_silent(
            self.run_hook(envelope("git commit -m x && gh pr merge --auto", stdout=out, cwd=repo))
        )

        with open(self.lock_log, "r", encoding="utf-8") as fh:
            answers = fh.read().split()
        # HEAD and the branch were asked of git (the origin is read from the config file itself,
        # no process), and every one of them before the lock was taken.
        self.assertGreaterEqual(len(answers), 2)
        self.assertEqual(set(answers), {"free"})
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.watch()), 1)

    def test_a_record_that_cannot_be_applied_does_not_cost_the_others(self):
        # A merge whose repo cannot be told (no remote, nothing named) and a commit beside it.
        plain = self.make_repo(name="plain", remote=None)
        out = self.commit_in(plain)
        self.assert_silent(
            self.run_hook(envelope("git commit -m x && gh pr merge 12", stdout=out, cwd=plain))
        )
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(self.watch(), [])


if __name__ == "__main__":
    unittest.main()
