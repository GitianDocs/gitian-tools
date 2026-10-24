#!/usr/bin/env python3
"""Unit tests for bash_actions.py, the gitian-kb nudge layer's reader of Bash commands.

bash_actions is pure (no subprocess, no filesystem, no state), so these call it directly: no hook
wrapper, no state file. HOME and CLAUDE_CONFIG_DIR are the only environment it reads and every test
pins them, so a developer's real ~/.claude never changes a verdict.

Most cases are here because a real command shape went wrong once or would have: a commit message
passed as `-m "$(cat <<'EOF' ... EOF)"`, a PR body that says "run git commit", a comment with an
apostrophe above the real command, `cd` inside a subshell.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_bash_actions.py
"""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOKS_DIR))

import bash_actions  # noqa: E402  (the module under test lives one directory up)

CWD = "/work/repo"
HOME = "/home/tester"


class ActionsTestCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"HOME": HOME})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def parse(self, command, cwd=CWD):
        return bash_actions.parse(command, cwd)

    def kinds(self, command, cwd=CWD):
        return [action["kind"] for action in self.parse(command, cwd)]

    def only(self, command, cwd=CWD):
        found = self.parse(command, cwd)
        self.assertEqual(len(found), 1, msg="%r -> %r" % (command, found))
        return found[0]

    def dirs(self, command, kind="commit", cwd=CWD):
        return [a["dir"] for a in self.parse(command, cwd) if a["kind"] == kind]

    def written(self, command, cwd=CWD):
        paths = []
        for action in self.parse(command, cwd):
            if action["kind"] == "write":
                paths.extend(action["paths"])
        return paths


class CommitActions(ActionsTestCase):
    def test_a_plain_commit_is_one_commit_action_in_the_cwd(self):
        self.assertEqual(
            self.parse("git commit -m 'fix things'"), [{"kind": "commit", "dir": CWD}]
        )

    def test_a_commit_is_found_wherever_it_sits_in_a_chain(self):
        for command in (
            "git add -A && git commit -m x",
            "git add -A; git commit -m x",
            "git diff --quiet || git commit -am x",
            "git add -A\ngit commit -m x",
            "git status | cat; git commit -m x",
            "git commit -m x &",
            "sleep 1 & git commit -m x",
            "cmd 2>&1 |& git commit -m x",
            "git add -A &&\n  git commit -m x",
            "git add -A && \\\n  git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.kinds(command), ["commit"])

    def test_commit_flag_and_path_spellings(self):
        for command in (
            "git commit",
            "git commit -am 'x'",
            "git commit --amend --no-edit",
            "git commit -F msg.txt",
            "/usr/bin/git commit -m x",
            "./git commit -m x",
            "git commit \\\n  -m x \\\n  -m y",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.kinds(command), ["commit"])

    def test_env_assignment_heads_are_skipped(self):
        for command in (
            "FOO=bar git commit -m x",
            "A=1 B=two git commit -m x",
            'GIT_AUTHOR_NAME="Some One" GIT_AUTHOR_EMAIL=a@b.c git commit -m x',
            "GIT_COMMITTER_DATE=2026-01-01T00:00:00Z /usr/bin/git commit",
            "A_1=x git -C sub commit",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.kinds(command), ["commit"])

    def test_commit_as_an_argument_or_lookalike_is_not_a_commit(self):
        for command in (
            "git log --grep commit",
            "git log --grep=commit --oneline",
            "git show commit",
            "git status",
            "git commit-tree HEAD^{tree}",
            "git commit-graph write",
            "npm run commit",
            "echo please git commit now",
            "git add -A && echo next step is git commit",
            "git",
            "commit",
            "gitk commit",
            "FOO=git commit",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.parse(command), [])

    def test_a_dry_run_is_not_a_commit(self):
        self.assertEqual(self.parse("git commit --dry-run -a"), [])

    def test_asking_for_help_is_not_a_commit(self):
        # It would name HEAD's existing commit as new (and could fire the first-work block).
        for command in ("git commit -h", "git commit --help"):
            with self.subTest(command=command):
                self.assertEqual(self.parse(command), [])

    def test_a_commit_inside_a_shell_construct_still_counts(self):
        for command in (
            "if git diff --quiet; then echo clean; else git commit -am x; fi",
            "for f in a b; do git commit -m $f; done",
            "! git commit -m x",
            "{ git add -A; git commit -m x; }",
            "(git add -A && git commit -m x)",
            "echo $(git commit -m x)",
            "out=$(git commit -m x 2>&1); echo $out",
            "case $x in a) git commit -m y ;; esac",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.kinds(command), ["commit"])


class GitGlobalOptions(ActionsTestCase):
    def test_options_before_the_subcommand_do_not_hide_a_commit(self):
        for command in (
            "git -C repo commit -m x",
            "git -c user.name=x commit",
            "git --git-dir /x/.git commit -m x",
            "git --git-dir=/x/.git commit -m x",
            "git --work-tree /x commit -m x",
            "git --work-tree=/x commit -m x",
            "git --namespace ns commit -m x",
            "git --no-pager commit -m x",
            "git -c commit.gpgsign=false -c user.email=a@b -C sub --no-pager commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.kinds(command), ["commit"])

    def test_an_option_argument_is_never_mistaken_for_the_subcommand(self):
        # `--git-dir` eats `commit`, so the subcommand is `log`; `-c commit` eats it likewise and
        # the real `commit` after it is the subcommand.
        self.assertEqual(self.parse("git --git-dir commit log"), [])
        self.assertEqual(self.parse("git -C commit log"), [])
        self.assertEqual(self.kinds("git -c commit commit"), ["commit"])
        # The `=` form is one token and consumes nothing extra.
        self.assertEqual(self.kinds("git --git-dir=commit commit"), ["commit"])

    def test_dash_capital_c_moves_the_directory_for_that_command_only(self):
        self.assertEqual(self.dirs("git -C /other commit -m a && git commit -m b"), ["/other", CWD])
        self.assertEqual(self.dirs("git -C sub commit"), [CWD + "/sub"])
        self.assertEqual(self.dirs("git -C ../sibling commit"), ["/work/sibling"])
        self.assertEqual(self.dirs("git -C ~/proj commit"), [HOME + "/proj"])
        self.assertEqual(self.dirs("git -C a -C b commit"), [CWD + "/a/b"])
        self.assertEqual(self.dirs("cd /a && git -C sub commit && git commit"), ["/a/sub", "/a"])

    def test_an_unknowable_dash_capital_c_keeps_the_directory(self):
        self.assertEqual(self.dirs('git -C "$REPO" commit'), [CWD])
        self.assertEqual(self.dirs("git -C $(git rev-parse --show-toplevel) commit"), [CWD])
        self.assertEqual(self.dirs("git -C ~nobody/x commit"), [CWD])


class QuotedMentionsAreInert(ActionsTestCase):
    def test_a_quoted_mention_is_never_an_action(self):
        for command in (
            'echo "git commit"',
            "echo 'gh pr merge 5'",
            'echo "gh release create v1.0.0"',
            'echo "x; git commit; y"',
            'echo "a && git commit"',
            'echo "a | git commit"',
            "echo 'one\ngit commit -m x\nthree'",
            'echo "one\ngit commit -m x\nthree"',
            'printf "%s\\n" "git commit"',
            'echo "cd /tmp && sed -i s/a/b/ file"',
            'git log --grep="git commit"',
            'echo "tee out.txt > file.txt"',
            "echo $'git commit; gh pr merge 5'",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.parse(command), [])

    def test_a_body_that_says_git_commit_is_not_a_second_action(self):
        self.assertEqual(
            self.kinds('gh pr create --title t --body "run git commit"'), ["pr_create"]
        )
        self.assertEqual(
            self.kinds('git commit -m "docs: explain gh pr merge 5; then git commit"'), ["commit"]
        )
        self.assertEqual(
            self.kinds('gh pr create --title t --body "step one\ngit commit -m x\nstep three"'),
            ["pr_create"],
        )

    def test_a_cd_inside_quotes_does_not_move_the_directory(self):
        self.assertEqual(self.dirs('echo "cd /elsewhere"; git commit'), [CWD])

    def test_a_comment_is_not_code_even_with_an_apostrophe_in_it(self):
        # The apostrophe would open a quote that swallows the real command below it.
        self.assertEqual(self.kinds("# don't forget this\ngit commit -m x"), ["commit"])
        self.assertEqual(self.kinds("git commit -m x # it's done\n"), ["commit"])
        self.assertEqual(self.kinds("git commit -m x # then; gh pr merge 5"), ["commit"])
        self.assertEqual(self.parse("# git commit -m x"), [])

    def test_a_hash_inside_a_word_is_not_a_comment(self):
        self.assertEqual(self.kinds("git commit -m fix#12; gh pr merge 3"), ["commit", "pr_merge"])

    def test_an_unterminated_quote_reads_as_nothing_from_that_quote_on(self):
        self.assertEqual(self.parse('git commit -m "unfinished'), [])
        self.assertEqual(self.parse("git commit -m 'unfinished"), [])
        self.assertEqual(self.parse("git commit -m $'unfinished"), [])
        self.assertEqual(self.parse('echo ok && git commit -m "oops'), [])
        # ...but a command that completed before the broken one is still seen.
        self.assertEqual(self.kinds('git commit -m done\necho "oops'), ["commit"])

    def test_quote_spellings_join_into_one_word(self):
        self.assertEqual(self.only("gh pr create -t 'it'\"'\"'s'")["title"], "it's")
        self.assertEqual(self.only('gh pr create -t "say \\"hi\\""')["title"], 'say "hi"')
        self.assertEqual(self.only("gh pr create -t it\\'s")["title"], "it's")
        self.assertEqual(self.only("gh pr create -t a\\ b")["title"], "a b")
        self.assertEqual(self.only("gh pr create -t 'a'\"b\"c")["title"], "abc")
        self.assertEqual(self.only("gh pr create -t $'it\\'s'")["title"], "it\\'s")
        self.assertEqual(self.kinds("git commit -m $'it\\'s done'"), ["commit"])


class Heredocs(ActionsTestCase):
    def test_a_heredoc_body_is_not_commands(self):
        self.assertEqual(
            self.parse("cat <<EOF > notes.md\ngit commit -m x\nEOF"),
            [{"kind": "write", "dir": CWD, "paths": [CWD + "/notes.md"]}],
        )
        self.assertEqual(
            self.parse("cat <<'EOF'\ngh pr merge 5\ngit commit -m x\ngh release create v1\nEOF"), []
        )

    def test_every_heredoc_spelling_hides_its_body(self):
        for command in (
            "cat <<EOF\ngit commit -m x\nEOF",
            "cat <<-EOF\n\tgit commit -m x\n\tEOF",
            "cat <<'EOF'\ngit commit -m x\nEOF",
            'cat <<"EOF"\ngit commit -m x\nEOF',
            "cat << EOF\ngit commit -m x\nEOF",
            "cat <<\\EOF\ngit commit -m x\nEOF",
            "cat <<EOF\r\ngit commit -m x\r\nEOF\r\n",
            "cat <<'END OF TEXT'\ngit commit -m x\nEND OF TEXT",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.parse(command), [])

    def test_the_command_after_a_heredoc_is_still_read(self):
        for command in (
            "cat <<EOF\nbody\nEOF\ngit commit -m real",
            "cat <<-EOF\n\tbody\n\tEOF\ngit commit -m real",
            "cat <<EOF && git commit -m real\nbody\nEOF",
            "git commit -F - <<'EOF'\nmessage\nEOF",
            "cat <<A <<B\nbody a\nA\nbody b\nB\ngit commit -m real",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.kinds(command), ["commit"])

    def test_only_a_line_that_is_exactly_the_delimiter_ends_the_body(self):
        self.assertEqual(
            self.kinds("cat <<EOF\nEOF2 is not the end\n  EOF\ngit commit -m inbody\nEOF\ngit commit"),
            ["commit"],
        )
        # `<<-` strips leading tabs only, not spaces.
        self.assertEqual(self.parse("cat <<-EOF\n  EOF\ngit commit -m inbody"), [])

    def test_a_heredoc_with_no_terminator_swallows_the_rest(self):
        self.assertEqual(self.parse("cat <<EOF\ngit commit -m x"), [])

    def test_here_strings_and_shifts_are_not_heredocs(self):
        self.assertEqual(self.kinds('cat <<< "hi"\ngit commit -m x'), ["commit"])
        self.assertEqual(self.kinds("echo $((1 << 2))\ngit commit -m x"), ["commit"])
        self.assertEqual(self.kinds('echo "a <<EOF"\ngit commit -m x'), ["commit"])
        self.assertEqual(self.kinds("echo 'a <<EOF'\ngit commit -m x"), ["commit"])

    def test_a_commit_message_passed_as_a_heredoc_substitution_is_a_commit(self):
        # How a multi-line message is almost always passed. The body (which may say anything,
        # quotes and unbalanced parentheses included) is not read; the commit is.
        message = (
            "git commit -m \"$(cat <<'EOF'\n"
            "feat: a thing\n"
            "\n"
            "Mentions git commit; gh pr merge 9 and a stray ) and an unbalanced \" quote.\n"
            "EOF\n"
            ')"'
        )
        self.assertEqual(self.parse(message), [{"kind": "commit", "dir": CWD}])
        self.assertEqual(self.kinds(message + " && git push"), ["commit"])
        self.assertEqual(self.kinds("git add -A && " + message), ["commit"])

    def test_a_pr_body_passed_as_a_heredoc_substitution_keeps_its_title(self):
        command = (
            'gh pr create --title "feat: x" --body "$(cat <<\'EOF\'\n'
            "## Summary\n"
            "- run git commit; gh pr merge 5\n"
            "EOF\n"
            ')"'
        )
        self.assertEqual(
            self.parse(command),
            [{"kind": "pr_create", "dir": CWD, "repo": None, "title": "feat: x", "head": None}],
        )

    def test_a_commit_then_a_pull_request_with_two_heredocs(self):
        command = (
            "git commit -m \"$(cat <<'EOF'\nmsg\nEOF\n)\" && "
            'gh pr create --title t --body "$(cat <<\'EOF\'\nbody\nEOF\n)"'
        )
        self.assertEqual(self.kinds(command), ["commit", "pr_create"])

    def test_a_heredoc_into_a_file_is_a_write(self):
        self.assertEqual(self.written("tee out.txt <<EOF\nx\nEOF"), [CWD + "/out.txt"])
        self.assertEqual(self.written("cat > a.txt <<'EOF'\nit's\nEOF\ngit commit"), [CWD + "/a.txt"])


class DirectoryTracking(ActionsTestCase):
    def test_cd_moves_the_directory_for_what_follows(self):
        self.assertEqual(self.dirs("cd /repo && git commit"), ["/repo"])
        self.assertEqual(self.dirs("cd /a; cd b; git commit"), ["/a/b"])
        self.assertEqual(self.dirs("git commit -m before && cd /after && git commit"), [CWD, "/after"])

    def test_a_relative_cd_resolves_against_the_effective_directory(self):
        self.assertEqual(self.dirs("cd sub && git commit"), [CWD + "/sub"])
        self.assertEqual(self.dirs("cd .. && git commit"), ["/work"])
        self.assertEqual(self.dirs("cd ../other && git commit"), ["/work/other"])
        self.assertEqual(self.dirs("cd ./sub/ && git commit"), [CWD + "/sub"])
        self.assertEqual(self.dirs("cd /a && cd ../b && git commit"), ["/b"])

    def test_tilde_and_home_expand(self):
        self.assertEqual(self.dirs("cd ~ && git commit"), [HOME])
        self.assertEqual(self.dirs("cd ~/proj && git commit"), [HOME + "/proj"])
        self.assertEqual(self.dirs("cd $HOME/proj && git commit"), [HOME + "/proj"])
        self.assertEqual(self.dirs("cd ${HOME}/proj && git commit"), [HOME + "/proj"])
        self.assertEqual(self.dirs('cd "$HOME/my proj" && git commit'), [HOME + "/my proj"])

    def test_a_bare_cd_goes_home(self):
        self.assertEqual(self.dirs("cd && git commit"), [HOME])
        self.assertEqual(self.dirs("cd /a && cd && git commit"), [HOME])

    def test_cd_dash_leaves_the_directory_unchanged(self):
        self.assertEqual(self.dirs("cd - && git commit"), [CWD])
        self.assertEqual(self.dirs("cd /a && cd - && git commit"), ["/a"])

    def test_cd_options_and_odd_operands(self):
        self.assertEqual(self.dirs("cd -P /x && git commit"), ["/x"])
        self.assertEqual(self.dirs("cd -- /x && git commit"), ["/x"])
        self.assertEqual(self.dirs('cd "/path with space" && git commit'), ["/path with space"])
        self.assertEqual(self.dirs('cd "" && git commit'), [CWD])

    def test_an_unknowable_cd_target_leaves_the_directory_alone(self):
        for command in (
            'cd "$REPO" && git commit',
            "cd $(mktemp -d) && git commit",
            "cd `pwd`/x && git commit",
            "cd ~someone/x && git commit",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.dirs(command), [CWD])

    def test_home_unset_makes_tilde_unknowable(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("HOME", None)
            self.assertEqual(self.dirs("cd ~ && git commit"), [CWD])
            self.assertEqual(self.dirs("cd && git commit"), [CWD])
            self.assertEqual(self.dirs("cd $HOME/x && git commit"), [CWD])

    def test_pushd_and_popd(self):
        self.assertEqual(self.dirs("pushd /a && git commit"), ["/a"])
        self.assertEqual(self.dirs("pushd sub >/dev/null && git commit"), [CWD + "/sub"])
        self.assertEqual(
            self.dirs("pushd /a && git commit && popd && git commit"), ["/a", CWD]
        )
        self.assertEqual(self.dirs("popd && git commit"), [CWD])
        self.assertEqual(
            self.dirs("pushd /a && pushd /b && popd && git commit && popd && git commit"),
            ["/a", CWD],
        )

    def test_a_subshell_or_substitution_does_not_leak_its_cd(self):
        self.assertEqual(self.dirs("(cd /x && git commit) && git commit"), ["/x", CWD])
        self.assertEqual(self.dirs("(cd /x); git commit"), [CWD])
        self.assertEqual(self.dirs("echo $(cd /x && pwd) && git commit"), [CWD])
        self.assertEqual(self.dirs("cd /a && (cd b && git commit); git commit"), ["/a/b", "/a"])
        self.assertEqual(self.dirs("(pushd /x; popd; git commit)"), [CWD])

    def test_a_missing_cwd_falls_back_to_dot(self):
        self.assertEqual(bash_actions.parse("git commit -m x", None), [{"kind": "commit", "dir": "."}])
        self.assertEqual(bash_actions.parse("git commit -m x", ""), [{"kind": "commit", "dir": "."}])
        self.assertEqual(bash_actions.parse("git commit -m x", 5), [{"kind": "commit", "dir": "."}])
        self.assertEqual(
            bash_actions.parse("cd sub && git commit", None), [{"kind": "commit", "dir": "sub"}]
        )


class PrCreate(ActionsTestCase):
    def test_title_repo_and_head_come_from_their_flags(self):
        self.assertEqual(
            self.parse("gh pr create --title 'feat: x' --body y"),
            [{"kind": "pr_create", "dir": CWD, "repo": None, "title": "feat: x", "head": None}],
        )
        self.assertEqual(
            self.parse("gh pr create --repo o/n --head feat/x -t T"),
            [{"kind": "pr_create", "dir": CWD, "repo": "o/n", "title": "T", "head": "feat/x"}],
        )

    def test_every_flag_spelling_reads_the_same(self):
        for flag, key, value in (
            ("--repo o/n", "repo", "o/n"),
            ("--repo=o/n", "repo", "o/n"),
            ("-R o/n", "repo", "o/n"),
            ("-Ro/n", "repo", "o/n"),
            ("-R=o/n", "repo", "o/n"),
            ("--title Tt", "title", "Tt"),
            ("--title=Tt", "title", "Tt"),
            ("-t Tt", "title", "Tt"),
            ("--head b", "head", "b"),
            ("--head=b", "head", "b"),
            ("-H b", "head", "b"),
            ("-H fork:b", "head", "fork:b"),
        ):
            with self.subTest(flag=flag):
                self.assertEqual(self.only("gh pr create " + flag)[key], value)

    def test_a_flag_value_is_never_read_as_another_flag(self):
        action = self.only('gh pr create --body "--title decoy -R x/y" --title real')
        self.assertEqual((action["title"], action["repo"]), ("real", None))
        action = self.only("gh pr create -b '-t decoy' -a me -l bug -r someone -B main -t real")
        self.assertEqual(action["title"], "real")

    def test_the_repo_value_is_reduced_to_owner_name_or_dropped(self):
        self.assertEqual(self.only("gh pr create -R github.com/o/n")["repo"], "o/n")
        self.assertEqual(self.only("gh pr create -R not-a-slug")["repo"], None)
        self.assertEqual(self.only("gh pr create -R 'a b/c'")["repo"], None)
        self.assertEqual(self.only("gh pr create -R $REPO")["repo"], None)

    def test_gh_repo_in_the_environment_names_the_repo(self):
        self.assertEqual(self.only("GH_REPO=o/n gh pr create -t x")["repo"], "o/n")
        self.assertEqual(self.only("GH_REPO=o/n gh pr create -R a/b")["repo"], "a/b")

    def test_unset_title_and_unknowable_values(self):
        self.assertEqual(self.only("gh pr create --fill")["title"], None)
        self.assertEqual(self.only("gh pr create --title ''")["title"], None)
        self.assertEqual(self.only('gh pr create --title "feat: $(date)"')["title"], "feat: $(...)")
        self.assertEqual(self.only("gh pr create --head $BRANCH")["head"], None)

    def test_other_gh_commands_are_not_pr_creates(self):
        for command in (
            "gh pr view 5",
            "gh pr list",
            "gh pr checkout 5",
            "gh pr",
            "gh",
            "gh issue create --title x",
            "gh repo create x",
            "gh api repos/o/n/pulls",
            "echo gh pr create --title x",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.parse(command), [])

    def test_gh_by_path_and_after_a_cd(self):
        self.assertEqual(self.kinds("/usr/bin/gh pr create -t x"), ["pr_create"])
        self.assertEqual(self.dirs("cd /r && gh pr create -t x", "pr_create"), ["/r"])


class PrMerge(ActionsTestCase):
    def test_a_number_a_hash_number_a_url_a_branch_or_nothing(self):
        def merge(command):
            action = self.only(command)
            self.assertEqual(action["kind"], "pr_merge")
            return (action["repo"], action["number"], action["branch"])

        self.assertEqual(merge("gh pr merge 123 --squash"), (None, 123, None))
        self.assertEqual(merge("gh pr merge --squash 123"), (None, 123, None))
        self.assertEqual(merge("gh pr merge '#650'"), (None, 650, None))
        self.assertEqual(merge("gh pr merge #650 --squash"), (None, 650, None))
        self.assertEqual(
            merge("gh pr merge https://github.com/o/n/pull/42"), ("o/n", 42, None)
        )
        self.assertEqual(
            merge("gh pr merge https://github.com/o/n/pull/42/files#diff --squash"),
            ("o/n", 42, None),
        )
        self.assertEqual(merge("gh pr merge feature/x"), (None, None, "feature/x"))
        self.assertEqual(merge("gh pr merge my-branch --squash"), (None, None, "my-branch"))
        self.assertEqual(merge("gh pr merge fork:feat"), (None, None, "fork:feat"))
        self.assertEqual(merge("gh pr merge"), (None, None, None))
        self.assertEqual(merge("gh pr merge --auto --squash"), (None, None, None))
        self.assertEqual(merge("gh pr merge $PR"), (None, None, None))

    def test_the_whole_action_shape(self):
        self.assertEqual(
            self.parse("gh pr merge 7 --auto"),
            [
                {
                    "kind": "pr_merge",
                    "dir": CWD,
                    "repo": None,
                    "number": 7,
                    "branch": None,
                    "auto": True,
                }
            ],
        )

    def test_auto_is_only_the_auto_flag(self):
        self.assertTrue(self.only("gh pr merge 7 --auto --squash")["auto"])
        self.assertTrue(self.only("gh pr merge --auto 7")["auto"])
        self.assertTrue(self.only("gh pr merge 7 --auto=true")["auto"])
        self.assertFalse(self.only("gh pr merge 7 --squash")["auto"])
        self.assertFalse(self.only("gh pr merge 7 --auto=false")["auto"])
        self.assertFalse(self.only("gh pr merge 7 --disable-auto")["auto"])
        self.assertFalse(self.only("gh pr merge 7 --body --auto")["auto"])

    def test_flag_values_are_not_the_positional(self):
        for command in (
            "gh pr merge --subject 'Title' 7",
            "gh pr merge -t 'Title' 7",
            "gh pr merge --body 'text' --squash 7",
            "gh pr merge -b 'text' 7",
            "gh pr merge --author-email a@b.c 7",
            "gh pr merge --match-head-commit abc123 7",
            "gh pr merge --squash --delete-branch -d 7",
        ):
            with self.subTest(command=command):
                action = self.only(command)
                self.assertEqual((action["number"], action["branch"]), (7, None))

    def test_repo_flag_and_environment(self):
        self.assertEqual(self.only("gh pr merge 7 -R o/n")["repo"], "o/n")
        self.assertEqual(self.only("gh pr merge --repo=o/n 7")["repo"], "o/n")
        self.assertEqual(self.only("GH_REPO=o/n gh pr merge 7")["repo"], "o/n")
        # A URL names its own repo and wins over the flag.
        self.assertEqual(
            self.only("gh pr merge -R a/b https://github.com/o/n/pull/42")["repo"], "o/n"
        )

    def test_a_number_too_long_to_be_a_pull_request_is_not_parsed_as_one(self):
        action = self.only("gh pr merge 99999999999999999999")
        self.assertEqual(action["number"], None)

    def test_not_a_merge(self):
        for command in (
            "gh pr view 7",
            "gh pr merged 7",
            "gh pr checks 7 --watch",
            "echo gh pr merge 7",
            "echo 'gh pr merge 7'",
            "gh issue merge 7",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.parse(command), [])

    def test_a_merge_after_a_chain_and_in_a_loop(self):
        self.assertEqual(self.kinds("gh pr checks 7 --watch && gh pr merge 7 --squash"), ["pr_merge"])
        self.assertEqual(self.kinds("for n in 1 2; do gh pr merge $n; done"), ["pr_merge"])


class Releases(ActionsTestCase):
    def test_the_whole_action_shape(self):
        self.assertEqual(
            self.parse("gh release create v1.2.3 --title 'v1.2.3' --notes 'n'"),
            [
                {
                    "kind": "release",
                    "dir": CWD,
                    "repo": None,
                    "tag": "v1.2.3",
                    "draft": False,
                    "edit": False,
                }
            ],
        )

    def test_draft_variants_on_create(self):
        for command, draft in (
            ("gh release create v1", False),
            ("gh release create v1 --draft", True),
            ("gh release create v1 -d", True),
            ("gh release create v1 --draft=true", True),
            ("gh release create --draft v1", True),
            ("gh release create v1 -dp", True),
            ("gh release create v1 -pd", True),
            ("gh release create v1 --draft=false", False),
            ("gh release create v1 --prerelease", False),
            ("gh release create v1 -p", False),
            ("gh release create v1 --notes '--draft'", False),
            ("gh release create v1 --title -d", False),
        ):
            with self.subTest(command=command):
                action = self.only(command)
                self.assertEqual((action["draft"], action["edit"]), (draft, False))

    def test_edit_to_undraft_is_a_release_and_every_other_edit_is_ignored(self):
        self.assertEqual(
            self.parse("gh release edit v1.2.3 --draft=false"),
            [
                {
                    "kind": "release",
                    "dir": CWD,
                    "repo": None,
                    "tag": "v1.2.3",
                    "draft": False,
                    "edit": True,
                }
            ],
        )
        self.assertEqual(self.kinds("gh release edit --draft=false v1.2.3 --latest"), ["release"])
        for command in (
            "gh release edit v1 --draft",
            "gh release edit v1 --draft=true",
            "gh release edit v1 --notes 'x'",
            "gh release edit v1 --latest",
            "gh release edit v1 --prerelease=false",
            "gh release edit v1",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.parse(command), [])

    def test_tag_repo_and_values_that_are_not_the_tag(self):
        self.assertEqual(self.only("gh release create --title 'Big one' -n 'x' v2.0.0")["tag"], "v2.0.0")
        self.assertEqual(self.only("gh release create v2.0.0 ./a.tgz ./b.tgz")["tag"], "v2.0.0")
        self.assertEqual(self.only("gh release create v2 -R o/n")["repo"], "o/n")
        self.assertEqual(self.only("gh release create v2 --repo=o/n")["repo"], "o/n")
        self.assertEqual(self.only("gh release create v2 --target main -F notes.md")["tag"], "v2")
        self.assertEqual(self.only("gh release create --generate-notes v2")["tag"], "v2")
        self.assertEqual(self.only("gh release create")["tag"], None)
        self.assertEqual(self.only("gh release create $TAG")["tag"], None)
        self.assertEqual(self.only('gh release create "v$VERSION"')["tag"], None)

    def test_other_release_subcommands_are_not_releases(self):
        for command in (
            "gh release upload v1 ./a.tgz",
            "gh release list",
            "gh release view v1",
            "gh release delete v1 --yes",
            "gh release download v1",
            "echo gh release create v1",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.parse(command), [])


class WriteSignatures(ActionsTestCase):
    def test_the_whole_action_shape(self):
        self.assertEqual(
            self.parse("sed -i 's/a/b/' src/x.ts docs/y.md"),
            [{"kind": "write", "dir": CWD, "paths": [CWD + "/src/x.ts", CWD + "/docs/y.md"]}],
        )

    def test_sed_in_place_spellings(self):
        for command in (
            "sed -i 's/a/b/' f.txt",
            "sed -i.bak 's/a/b/' f.txt",
            "sed -Ei 's/a/b/' f.txt",
            "sed -ni 's/a/b/p' f.txt",
            "sed -i -E 's/a/b/' f.txt",
            "sed --in-place 's/a/b/' f.txt",
            "sed --in-place=.bak 's/a/b/' f.txt",
            "sed -i -e 's/a/b/' f.txt",
            "sed -i -e 's/a/b/' -e 's/c/d/' f.txt",
            "sed -i --expression='s/a/b/' f.txt",
            "sed -i --expression 's/a/b/' f.txt",
            "sed -i -f script.sed f.txt",
            "sed -s -i 's/a/b/' f.txt",
            "sed -ie 's/a/b/' f.txt",
            "/usr/bin/sed -i 's/a/b/' f.txt",
            "sed -i -- 's/a/b/' f.txt",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.written(command), [CWD + "/f.txt"])

    def test_bsd_sed_empty_backup_suffix_is_not_the_script_or_a_file(self):
        # macOS spelling: `sed -i '' 's/a/b/' file`. The empty word is the suffix.
        self.assertEqual(self.written("sed -i '' 's/a/b/' f.txt"), [CWD + "/f.txt"])
        self.assertEqual(self.written("sed -i '' -e 's/a/b/' f.txt"), [CWD + "/f.txt"])
        self.assertEqual(self.written("sed -i '' -E 's/a/b/' f.txt g.txt"), [CWD + "/f.txt", CWD + "/g.txt"])

    def test_sed_without_an_in_place_flag_writes_nothing(self):
        for command in (
            "sed 's/a/b/' f.txt",
            "sed -n '1,5p' f.txt",
            "sed -e 's/a/b/' f.txt",
            "sed -E 's/a/b/' f.txt > /tmp/out.txt",
            "echo sed -i x f",
            "sed --version",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.written(command), [])

    def test_sed_script_is_not_a_file(self):
        self.assertEqual(self.written("sed -i 's/a/b/' one two"), [CWD + "/one", CWD + "/two"])
        self.assertEqual(self.written("sed -i -e 's/a/b/' one two"), [CWD + "/one", CWD + "/two"])
        self.assertEqual(self.written("sed -i 's/a/b/'"), [])

    def test_perl_in_place_spellings(self):
        for command in (
            "perl -i -pe 's/a/b/' f.txt",
            "perl -pi -e 's/a/b/' f.txt",
            "perl -p -i -e 's/a/b/' f.txt",
            "perl -i.bak -pe 's/a/b/' f.txt",
            "perl -pi.bak -e 's/a/b/' f.txt",
            "perl -0777 -pi -e 's/a/b/' f.txt",
            "perl -lpi -e 's/a/b/' f.txt",
            "perl -pi -e 's/a/b/' -- f.txt",
            "perl -MFile::Basename -pi -e 's/a/b/' f.txt",
            "/usr/bin/perl -pi -e 's/a/b/' f.txt",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.written(command), [CWD + "/f.txt"])
        self.assertEqual(self.written("perl -pi -e 's/a/b/' x y"), [CWD + "/x", CWD + "/y"])

    def test_perl_without_an_in_place_flag_writes_nothing(self):
        for command in (
            "perl -e 'print 1'",
            "perl -pe 's/a/b/' f.txt",
            "perl -ne 'print if /x/' f.txt",
            "perl script.pl f.txt",
            "perl --version",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.written(command), [])

    def test_tee_targets(self):
        self.assertEqual(self.written("echo hi | tee out.txt"), [CWD + "/out.txt"])
        self.assertEqual(self.written("echo hi | tee -a out.txt"), [CWD + "/out.txt"])
        self.assertEqual(self.written("echo hi | tee --append a b"), [CWD + "/a", CWD + "/b"])
        self.assertEqual(self.written("echo hi | tee -- -odd"), [CWD + "/-odd"])
        self.assertEqual(self.written("echo hi | tee"), [])
        self.assertEqual(self.written("echo hi | tee -a"), [])
        self.assertEqual(self.written("echo hi | tee -"), [])

    def test_redirection_to_a_file(self):
        for command in (
            "echo hi > out.txt",
            "echo hi >> out.txt",
            "echo hi >out.txt",
            "echo hi>out.txt",
            "echo hi >>out.txt",
            "echo hi >| out.txt",
            "> out.txt",
            ": > out.txt",
            "cat in.txt > out.txt",
            "cat <in.txt >out.txt",
            "echo hi >'out.txt'",
            'echo hi > "out.txt"',
            "FOO=bar echo hi > out.txt",
            "echo hi > out.txt 2>&1",
            "echo hi 2>&1 > out.txt",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.written(command), [CWD + "/out.txt"])

    def test_redirections_that_are_plumbing_not_writes(self):
        for command in (
            "cmd >&2",
            "cmd > &2",
            "cmd 2>&1",
            "cmd 1>&2",
            "cmd 2>/dev/null",
            "cmd 2> err.log",
            "cmd 1> out.log",
            "cmd &>/dev/null",
            "cmd &> all.log",
            "cmd &>> all.log",
            "cmd >& all.log",
            "cmd < in.txt",
            "cmd <<< hi",
            "exec 3>&1",
            "cmd 3<&0",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.written(command), [])

    def test_a_quoted_or_arithmetic_greater_than_is_not_a_redirection(self):
        for command in (
            "grep '>' file",
            'grep ">" file',
            'echo "a > b"',
            "echo a \\> b",
            "[[ $a > b ]] && echo yes",
            "[ 3 > 2 ]",
            "(( x > 3 )) && echo big",
            "echo $((x > 3))",
            'echo "$((x > 3))"',
            "if [[ $x > 5 ]]; then echo; fi",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.written(command), [])

    def test_patch_and_git_apply_write_to_the_working_tree(self):
        self.assertEqual(self.written("patch -p1 < fix.diff"), [CWD])
        self.assertEqual(self.written("patch -p1 -i fix.diff"), [CWD])
        self.assertEqual(self.written("git apply fix.patch"), [CWD])
        self.assertEqual(self.written("git apply --index fix.patch"), [CWD])
        self.assertEqual(self.written("git apply fix.patch /tmp/other.patch"), [CWD])
        self.assertEqual(self.written("cd /r && git apply fix.patch"), ["/r"])
        self.assertEqual(self.written("git -C /other apply fix.patch"), ["/other"])

    def test_dry_runs_and_reports_of_patches_write_nothing(self):
        for command in (
            "git apply --check fix.patch",
            "git apply --stat fix.patch",
            "git apply --numstat fix.patch",
            "git apply --summary fix.patch",
            "patch --dry-run -p1 < fix.diff",
            "patch --check -p1 < fix.diff",
            "git applypatch x",
        ):
            with self.subTest(command=command):
                self.assertEqual(self.written(command), [])

    def test_a_write_resolves_against_the_effective_directory(self):
        self.assertEqual(self.written("cd /repo && echo x > a.txt"), ["/repo/a.txt"])
        self.assertEqual(self.written("cd sub && sed -i s/a/b/ f"), [CWD + "/sub/f"])
        self.assertEqual(self.written("echo x > ../other/f"), ["/work/other/f"])
        self.assertEqual(self.written("echo x > ./a/../b.txt"), [CWD + "/b.txt"])
        self.assertEqual(self.written("echo x > /abs/f"), ["/abs/f"])
        self.assertEqual(self.written("echo x > ~/f"), [HOME + "/f"])
        self.assertEqual(self.written("echo x > $HOME/f"), [HOME + "/f"])
        self.assertEqual(self.written("(cd /x && echo y > f); echo z > g"), ["/x/f", CWD + "/g"])
        self.assertEqual(self.dirs("cd /repo && echo x > a.txt", "write"), ["/repo"])

    def test_a_target_that_cannot_be_resolved_is_dropped_not_guessed(self):
        # A redirect or tee target may well be a temp file; guessing a path would be a false edit.
        self.assertEqual(self.parse('echo x > "$OUT"'), [])
        self.assertEqual(self.parse("echo x > $(mktemp)"), [])
        self.assertEqual(self.parse("echo x | tee ~someone/f"), [])
        self.assertEqual(self.written("echo x | tee $LOG real.txt >/dev/null"), [CWD + "/real.txt"])

    def test_an_in_place_edit_of_files_the_text_cannot_name_counts_as_the_directory(self):
        # `sed -i` only edits files that exist, found by a listing in the tree it runs in.
        self.assertEqual(self.written("sed -i s/a/b/ $FILE"), [CWD])
        self.assertEqual(self.written("sed -i s/a/b/ $(git ls-files '*.ts')"), [CWD])
        self.assertEqual(self.written('for f in *.ts; do sed -i s/a/b/ "$f"; done'), [CWD])
        self.assertEqual(self.written("cd /r && perl -pi -e 's/a/b/' $FILES"), ["/r"])
        self.assertEqual(self.written('sed -i s/a/b/ "$F" real.txt'), [CWD, CWD + "/real.txt"])
        self.assertEqual(self.parse("cd /tmp && sed -i s/a/b/ $FILE"), [])

    def test_scratch_and_device_targets_are_not_work(self):
        for target in (
            "/tmp/x.txt",
            "/tmp/dir/deep/x.txt",
            "/var/tmp/x.txt",
            "/dev/null",
            "/dev/stdout",
            "/proc/self/oom_score_adj",
            "/tmp/../tmp/x.txt",
        ):
            with self.subTest(target=target):
                self.assertEqual(self.parse("echo x > " + target), [])
                self.assertEqual(self.parse("echo x | tee " + target), [])
                self.assertEqual(self.parse("sed -i s/a/b/ " + target), [])
        # An effective directory in scratch space makes a relative target scratch too.
        self.assertEqual(self.parse("cd /tmp && echo x > f"), [])
        # A name that merely starts like a scratch dir is not one.
        self.assertEqual(self.written("echo x > /tmpfile"), ["/tmpfile"])
        self.assertEqual(self.written("echo x > /devices/x"), ["/devices/x"])
        # `..` is folded before the check, so it cannot smuggle a target out of scratch.
        self.assertEqual(self.written("echo x > /tmp/../repo/x"), ["/repo/x"])

    def test_claude_code_state_is_not_work_under_the_default_config_dir(self):
        for target in (
            HOME + "/.claude/projects/-work-repo/memory/note.md",
            HOME + "/.claude/projects/x",
            HOME + "/.claude/plans/p.md",
            "~/.claude/projects/-work-repo/memory/MEMORY.md",
            "~/.claude/plans/p.md",
        ):
            with self.subTest(target=target):
                self.assertEqual(self.parse("echo x > " + target), [])
        # Authored source under the same directory still counts.
        for target in ("~/.claude/skills/s/SKILL.md", "~/.claude/agents/a.md", "~/.claude/CLAUDE.md"):
            with self.subTest(target=target):
                self.assertEqual(len(self.parse("echo x > " + target)), 1)

    def test_claude_config_dir_moves_the_exemption(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/cfg"}):
            self.assertEqual(self.parse("echo x > /cfg/projects/-work-repo/memory/n.md"), [])
            self.assertEqual(self.parse("echo x > /cfg/plans/p.md"), [])
            self.assertEqual(self.parse("echo x | tee /cfg/projects/a"), [])
            self.assertEqual(self.written("echo x > /cfg/skills/s/SKILL.md"), ["/cfg/skills/s/SKILL.md"])
            # The default location is an ordinary directory once the variable points elsewhere.
            self.assertEqual(
                self.written("echo x > ~/.claude/projects/a"), [HOME + "/.claude/projects/a"]
            )
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "/cfg/"}):
            self.assertEqual(self.parse("echo x > /cfg/projects/a"), [])
        # A relative value is ignored, exactly as orientation-check.sh ignores it.
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "rel"}):
            self.assertEqual(self.written("echo x > rel/projects/a"), [CWD + "/rel/projects/a"])

    def test_only_the_paths_that_survive_are_reported(self):
        self.assertEqual(self.written("tee /tmp/a ./b"), [CWD + "/b"])
        self.assertEqual(self.written("tee a /dev/null b"), [CWD + "/a", CWD + "/b"])
        self.assertEqual(self.parse("tee /tmp/a /dev/null"), [])
        self.assertEqual(self.written("tee a a ./a"), [CWD + "/a"])

    def test_a_redirect_and_a_command_write_make_one_action(self):
        self.assertEqual(
            self.parse("sed -i s/a/b/ f > g"),
            [{"kind": "write", "dir": CWD, "paths": [CWD + "/f", CWD + "/g"]}],
        )

    def test_a_commit_with_its_output_redirected(self):
        self.assertEqual(self.kinds("git commit -m x > commit.log"), ["commit", "write"])
        self.assertEqual(self.kinds("git commit -m x > /tmp/commit.log 2>&1"), ["commit"])

    def test_wrappers_are_not_looked_through(self):
        # Documented limit: only the command word itself counts.
        for command in ("sudo sed -i s/a/b/ f", "env sed -i s/a/b/ f", "nice tee out.txt"):
            with self.subTest(command=command):
                self.assertEqual(self.written(command), [])


class Segments(ActionsTestCase):
    def test_actions_come_back_in_command_order(self):
        command = "git commit -m x && gh pr create --title t && gh pr merge 5 --auto"
        self.assertEqual(self.kinds(command), ["commit", "pr_create", "pr_merge"])
        command = "echo a > f; git commit -m x; gh release create v1 --draft"
        self.assertEqual(self.kinds(command), ["write", "commit", "release"])

    def test_every_separator_splits(self):
        for separator in (" && ", " || ", " | ", " |& ", "; ", "\n", " & ", " ;\n "):
            with self.subTest(separator=separator):
                command = "echo a" + separator + "gh pr merge 5" + separator + "echo b"
                self.assertEqual(self.kinds(command), ["pr_merge"])

    def test_a_separator_without_spaces_still_splits(self):
        self.assertEqual(self.kinds("echo a;gh pr merge 5;echo b"), ["pr_merge"])
        self.assertEqual(self.kinds("echo a&&git commit"), ["commit"])
        self.assertEqual(self.kinds("echo a||git commit"), ["commit"])
        self.assertEqual(self.kinds("echo a|git commit"), ["commit"])

    def test_a_pull_request_url_in_a_substitution_or_process_substitution(self):
        self.assertEqual(self.kinds("echo $(gh pr merge 5)"), ["pr_merge"])
        self.assertEqual(self.kinds("echo `gh pr merge 5`"), ["pr_merge"])
        self.assertEqual(self.kinds("diff <(git show a) <(git show b) && git commit -m x"), ["commit"])

    def test_a_substitution_holding_a_command_does_not_hide_the_outer_command(self):
        self.assertEqual(self.kinds("git commit -m \"$(git log -1 --format=%s)\""), ["commit"])
        self.assertEqual(self.kinds("V=$(git describe --tags) && git commit -m $V"), ["commit"])
        self.assertEqual(self.kinds("gh pr merge $(gh pr list --json number -q '.[0].number')"), ["pr_merge"])

    def test_a_line_continuation_joins_lines(self):
        self.assertEqual(self.kinds("gh pr \\\n  merge 5 \\\n  --auto"), ["pr_merge"])
        self.assertTrue(self.only("gh pr \\\n  merge 5 \\\n  --auto")["auto"])

    def test_words_are_whatever_the_shell_would_pass(self):
        # Quoted words keep their spaces; an escaped space joins; adjacent quotes concatenate.
        self.assertEqual(self.only('gh pr create -t "two words"')["title"], "two words")
        self.assertEqual(self.only("gh pr create -t two\\ words")["title"], "two words")
        self.assertEqual(self.only("gh pr create -t 'a'\"b\"")["title"], "ab")

    def test_non_commands_are_empty(self):
        for command in ("", "   ", "\n\n", "echo hello", "ls -la", "# only a comment", "true"):
            with self.subTest(command=command):
                self.assertEqual(self.parse(command), [])


class ResponseText(unittest.TestCase):
    def test_a_string_is_returned_as_it_is(self):
        self.assertEqual(bash_actions.response_text("fatal: nope"), "fatal: nope")
        self.assertEqual(bash_actions.response_text(""), "")

    def test_a_bash_response_dict_is_stdout_newline_stderr(self):
        response = {"stdout": "out text", "stderr": "err text", "interrupted": False, "isImage": False}
        self.assertEqual(bash_actions.response_text(response), "out text\nerr text")
        self.assertEqual(bash_actions.response_text({"stdout": "", "stderr": ""}), "\n")

    def test_a_dict_with_only_one_stream(self):
        self.assertEqual(bash_actions.response_text({"stdout": "only out"}), "only out\n")
        self.assertEqual(bash_actions.response_text({"stderr": "only err"}), "\nonly err")
        self.assertEqual(bash_actions.response_text({"stdout": None, "stderr": "e"}), "\ne")

    def test_a_dict_without_streams_is_json_rendered(self):
        response = {"output": "error: from some other shape", "code": 1}
        self.assertEqual(bash_actions.response_text(response), json.dumps(response))

    def test_a_list_is_json_rendered(self):
        response = [{"type": "text", "text": "fatal: not a repository"}]
        text = bash_actions.response_text(response)
        self.assertEqual(text, json.dumps(response))
        self.assertTrue(bash_actions.commit_failed(text))

    def test_nothing_and_unserializable_values_are_empty_text(self):
        self.assertEqual(bash_actions.response_text(None), "")
        self.assertEqual(bash_actions.response_text({1, 2}), "")
        self.assertEqual(bash_actions.response_text(7), "7")


class FailurePredicates(unittest.TestCase):
    def test_commit_failed_markers_are_case_insensitive(self):
        for text in (
            "nothing to commit, working tree clean",
            "On branch main\nNothing to commit",
            "error: pathspec 'x' did not match any file(s) known to git",
            "ERROR: nope",
            "fatal: not a git repository",
            "FATAL: bad",
            "Aborting commit due to empty commit message.",
            "hint: ...\naborting commit",
        ):
            with self.subTest(text=text):
                self.assertTrue(bash_actions.commit_failed(text))

    def test_a_successful_commit_is_not_a_failure(self):
        for text in (
            "[main 1a2b3c4] fix things\n 1 file changed, 2 insertions(+)",
            "",
            "On branch main\nChanges to be committed:",
        ):
            with self.subTest(text=text):
                self.assertFalse(bash_actions.commit_failed(text))

    def test_commit_failed_ignores_what_is_not_text(self):
        for value in (None, 7, ["error:"], {"stderr": "error:"}):
            with self.subTest(value=value):
                self.assertFalse(bash_actions.commit_failed(value))

    def test_a_dict_response_flows_through_response_text(self):
        failed = {"stdout": "", "stderr": "fatal: not a git repository", "interrupted": False}
        ok = {"stdout": "[main 1a2b3c4] x\n", "stderr": "", "interrupted": False}
        self.assertTrue(bash_actions.commit_failed(bash_actions.response_text(failed)))
        self.assertFalse(bash_actions.commit_failed(bash_actions.response_text(ok)))

    def test_release_failed_markers_are_case_insensitive(self):
        for text in (
            "error: unknown flag --nope",
            "ERROR: x",
            "fatal: no such tag",
            "HTTP 422: Validation Failed (https://api.github.com/repos/o/n/releases)",
            "http 404",
            "HTTP 502: Bad Gateway",
            "GraphQL: Could not resolve to a Repository",
            "Validation Failed: a release with the same tag name already exists",
            "Release.tag_name already_exists\nALREADY EXISTS",
            "release not found",
            "Not Found",
        ):
            with self.subTest(text=text):
                self.assertTrue(bash_actions.release_failed(text))

    def test_a_published_release_is_not_a_failure(self):
        for text in (
            "https://github.com/o/n/releases/tag/v1.2.3\n",
            "",
            "HTTP 200",
            "HTTP 301 moved",
        ):
            with self.subTest(text=text):
                self.assertFalse(bash_actions.release_failed(text))

    def test_release_failed_ignores_what_is_not_text(self):
        for value in (None, 7, ["not found"]):
            with self.subTest(value=value):
                self.assertFalse(bash_actions.release_failed(value))


class FindUrls(unittest.TestCase):
    def test_find_pr_url_returns_repo_and_number(self):
        self.assertEqual(
            bash_actions.find_pr_url("https://github.com/o/n/pull/650\n"), ("o/n", 650)
        )
        self.assertEqual(
            bash_actions.find_pr_url("Creating pull request for feat into main in o/n\n\nhttps://github.com/Some-Org/my.repo_1/pull/7\n"),
            ("Some-Org/my.repo_1", 7),
        )

    def test_find_pr_url_takes_the_first_pull_request_url(self):
        text = "see https://github.com/a/b/pull/1 and https://github.com/c/d/pull/2"
        self.assertEqual(bash_actions.find_pr_url(text), ("a/b", 1))
        self.assertEqual(
            bash_actions.find_pr_url("https://github.com/a/b/issues/3 https://github.com/c/d/pull/4"),
            ("c/d", 4),
        )

    def test_find_pr_url_reads_a_url_with_a_trailing_path_or_punctuation(self):
        self.assertEqual(bash_actions.find_pr_url("(https://github.com/o/n/pull/9/files)"), ("o/n", 9))
        self.assertEqual(bash_actions.find_pr_url("merged https://github.com/o/n/pull/9."), ("o/n", 9))

    def test_find_pr_url_finds_nothing_in_other_text(self):
        for text in (
            "",
            "no url here",
            "https://github.com/o/n/pull/1234567890",  # not a pull request number
            "https://github.com/o/n/issues/5",
            "https://github.com/o/n/pulls",
            "https://gitlab.com/o/n/pull/5",
            "https://github.com/o/pull/5",
            None,
            7,
        ):
            with self.subTest(text=text):
                self.assertIsNone(bash_actions.find_pr_url(text))

    def test_find_release_url_returns_repo_and_tag(self):
        self.assertEqual(
            bash_actions.find_release_url("https://github.com/o/n/releases/tag/v0.7.6\n"),
            ("o/n", "v0.7.6"),
        )
        self.assertEqual(
            bash_actions.find_release_url("https://github.com/o/n/releases/tag/untagged-abc123def"),
            ("o/n", "untagged-abc123def"),
        )

    def test_find_release_url_strips_punctuation_and_decodes_the_tag(self):
        self.assertEqual(
            bash_actions.find_release_url("published at https://github.com/o/n/releases/tag/v1.0."),
            ("o/n", "v1.0"),
        )
        self.assertEqual(
            bash_actions.find_release_url('"https://github.com/o/n/releases/tag/v1.0"'),
            ("o/n", "v1.0"),
        )
        self.assertEqual(
            bash_actions.find_release_url("https://github.com/o/n/releases/tag/release%2F1.0"),
            ("o/n", "release/1.0"),
        )

    def test_find_release_url_takes_the_first_and_finds_nothing_else(self):
        text = "https://github.com/a/b/releases/tag/v1 https://github.com/c/d/releases/tag/v2"
        self.assertEqual(bash_actions.find_release_url(text), ("a/b", "v1"))
        for text in (
            "",
            "https://github.com/o/n/releases/latest",
            "https://github.com/o/n/releases",
            "https://github.com/o/n/pull/5",
            "https://github.com/o/n/releases/tag/",
            None,
            7,
        ):
            with self.subTest(text=text):
                self.assertIsNone(bash_actions.find_release_url(text))


class FailOpen(ActionsTestCase):
    def test_what_is_not_a_command_string_yields_nothing(self):
        for value in (None, 7, 1.5, [], ["git commit"], {}, b"git commit", True):
            with self.subTest(value=value):
                self.assertEqual(bash_actions.parse(value, CWD), [])

    def test_hostile_text_never_raises(self):
        for command in (
            "(((",
            ")))",
            "$(",
            "$((",
            "`",
            "``",
            '"',
            "'",
            "\\",
            "<<",
            "<<EOF",
            ">",
            "&",
            "&&",
            "|",
            ";",
            "$'",
            "${",
            "<(",
            ">(",
            "2>",
            "git commit -m \"$(",
            "\x00\x01\x02",
            "git commit " + "(" * 500,
            "$(" * 500,
            "`" * 501,
        ):
            with self.subTest(command=command[:40]):
                self.assertIsInstance(bash_actions.parse(command, CWD), list)

    def test_absurd_nesting_is_given_up_on(self):
        self.assertEqual(self.parse("(" * 100 + "git commit" + ")" * 100), [])
        self.assertEqual(self.parse("echo " + "$(" * 100 + "git commit" + ")" * 100), [])
        # Plausible nesting is fine.
        self.assertEqual(self.kinds("(echo a; (git commit -m x))"), ["commit"])
        self.assertEqual(self.kinds("echo $(echo $(echo $(git commit -m x)))"), ["commit"])
        # Documented limit: a leading `((` is arithmetic, as it is to bash, and holds no commands.
        self.assertEqual(self.parse("((git commit -m x))"), [])

    def test_an_absurdly_long_command_is_skipped(self):
        long_command = "echo " + "a" * bash_actions.MAX_COMMAND_CHARS + "\ngit commit -m x"
        self.assertEqual(self.parse(long_command), [])

    def test_a_large_heredoc_does_not_slow_the_hook_down(self):
        body = "a line with 'quotes\" and $(substitutions) and ) parens (\n" * 5000
        command = "cat > f.txt <<'EOF'\n" + body + "EOF\ngit commit -m x"
        self.assertEqual(self.kinds(command), ["write", "commit"])

    def test_the_module_is_pure(self):
        source = (HOOKS_DIR / "bash_actions.py").read_text(encoding="utf-8")
        for needle in ("import subprocess", "from subprocess", "import sys", "open(", "os.path.exists"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, source)


if __name__ == "__main__":
    unittest.main()
