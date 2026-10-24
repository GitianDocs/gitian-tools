#!/usr/bin/env python3
"""bash_activity.py -- PostToolUse hook: record what a Bash command DID in the session's ledger.

Invoked by bash-activity.sh (matcher "Bash") after every Bash call. Most of a long session's work
goes through Bash, not through the Edit tool: the 14-hour session that motivated 0.33.0 made 249
Bash calls against 15 Edit/Write calls, and every commit, pull request, merge and the production
release in it was a command. This hook turns the ones that matter into ledger activity (ledger.py)
so that a checkpoint can say WHAT is unwritten -- and that is all it does. It is RECORD-ONLY and
never prints. The nudge it replaces (commit_nudge.py) spoke the moment a commit landed, before a
compliant agent could have briefed the scribe, and latched after one use; the checkpoints (Stop, the
user's next prompt, SessionStart) read the ledger later, once the agent has had its turn, which is
what keeps a compliant session silent (tests/test_invariant.py).

bash_actions.parse reads the command; this module records what it found, consulting the command's
OUTPUT only where the output decides:

  commit     a `commit` event: HEAD's short sha and subject in the directory the commit ran in.
             Skipped when the output reads as a failure (nothing to commit, an error, an abort).
  pr create  with the pull request's URL in the output: watch it by number (why "opened") and record
             a `pr` event. Without one (a failed or silent create): watch the branch, no event.
  pr merge   watch the pull request, by number, else by branch name, else by the current branch:
             why "armed" for `--auto`, why "merge" for a plain merge. NEVER an event: `--auto` only
             ARMS a merge, the real one happens on GitHub long after, and a plain merge is confirmed
             by the Stop poll (pr_poll.py) too -- the poll is the only source of merges. The why
             matters to ledger.apply_poll: a merge the session made itself is stamped with GitHub's
             mergedAt, so a brief written right after it covers it; an armed one is stamped when the
             poll saw it.
  release    a `release` event, flagged draft for `--draft`, for `gh release create` and for
             `gh release edit --draft=false` (publishing a draft). Skipped on failure output.
  write      one edit in the ledger's per-minute bucket for the repo the first target lives in
             (`sed -i`, `perl -i`, `tee`, `>`/`>>`, `patch`, `git apply`). Scratch locations and
             Claude Code's own projects/ and plans/ never get here: bash_actions drops them.

The repo of a record is the one the command NAMES (`--repo`, `GH_REPO`), else the one its output
names (a pull request or release URL, which is the right answer for a fork), else the `origin` of
the directory the command ran in (ledger.locate).

Cheap by design. A command with no action (nearly every one) returns after a pure parse, before the
ledger is even imported, let alone the state file or a subprocess touched. When there IS something
to record, every git question (locate, head_commit, current_branch: subprocesses with a 2 s cap) is
answered BEFORE the lock, then ONE ledger.mutate_session applies all the records: the state lock
serializes every hook of every session on the machine and must never wait on git. A record that
cannot land (a bare `gh pr merge` on a detached HEAD, a failed commit) writes nothing at all.

A subagent's Bash calls fire this hook under the parent's session id, and its work is the session's
work: they are recorded exactly like the primary's. Only the transcript path is left alone for them
(ledger.note_transcript is for the primary's, so a /clear can name the right file to a scribe).

Fail-open, always: bad stdin, a missing session id, a corrupt or unwritable state file, a failing
git or any other error is swallowed (per action, then by the top-level guard below) and the process
exits 0 having printed nothing.

Run directly: python3 bash_activity.py < post_tool_use_envelope.json
Tests: plugins/gitian-kb/hooks/tests/test_bash_activity.py (drives it via bash-activity.sh, end to
end).
"""

import json
import sys

# bash_activity.py always runs as a script file (never `python3 -c ...`), so Python has already put
# its own directory at sys.path[0] -- the sibling imports resolve directly (the same convention
# harvest.py documents). `ledger` is imported later, in main(): see there.
import bash_actions


def _parse_stdin():
    """Read the whole hook-input envelope. Unparsable/non-object stdin -> {}."""
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    return payload


def _title(raw):
    """A `--title` worth showing. bash_actions hands back what the command computed unexpanded
    (`$TITLE`, `$(...)`), and quoting that in a nudge helps nobody."""
    return raw if isinstance(raw, str) and "$" not in raw else None


class _Resolver(object):
    """Turns each action into a RECORD: a function (session, now) that writes it into the ledger.
    Every git question is answered here, while no lock is held, and memoized for the run (a command
    that commits twice in one directory asks once). `text` is the command's flattened output."""

    def __init__(self, ledger, text):
        self.ledger = ledger
        self.text = text
        self._heads = {}
        self._branches = {}
        self._handlers = {
            "commit": self.commit,
            "pr_create": self.pr_create,
            "pr_merge": self.pr_merge,
            "release": self.release,
            "write": self.write,
        }

    def resolve(self, action):
        """The record for one action, or None: nothing to record (a failed commit, a record that
        could not land) or anything that went wrong asking git. Never raises."""
        handler = self._handlers.get(action.get("kind"))
        try:
            return handler(action) if handler else None
        except Exception:
            return None

    # --- git questions -------------------------------------------------------------------------

    def head(self, directory):
        if directory not in self._heads:
            self._heads[directory] = self.ledger.head_commit(directory)
        return self._heads[directory]

    def branch(self, directory):
        if directory not in self._branches:
            self._branches[directory] = self.ledger.current_branch(directory)
        return self._branches[directory]

    def repo(self, action, named):
        """(repo, where): the repo the action names, else `named` (what the output names), else the
        checkout the command ran in -- whose `where` labels a directory with no usable remote."""
        repo = action.get("repo") or named
        if repo:
            return repo, None
        return self.ledger.locate(action["dir"])

    # --- one method per action kind ------------------------------------------------------------

    def commit(self, action):
        if bash_actions.commit_failed(self.text):
            return None
        directory = action["dir"]
        repo, where = self.ledger.locate(directory)
        sha, subject = self.head(directory)
        return lambda session, now: self.ledger.record_event(
            session, now, "commit", repo=repo, where=where, ref=sha, text=subject
        )

    def pr_create(self, action):
        found = bash_actions.find_pr_url(self.text)  # (repo, number) the create printed, or None
        repo, where = self.repo(action, found[0] if found else None)
        if found:
            number, title = found[1], _title(action.get("title"))

            def record(session, now):
                self.ledger.add_watch(session, now, repo, number=number, why="opened")
                self.ledger.record_event(
                    session, now, "pr", repo=repo, where=where, ref="#%d" % number, text=title
                )

            return record
        branch = action.get("head") or self.branch(action["dir"])
        if not repo or not branch:
            return None  # a watch needs both; without them there is nothing to write
        return lambda session, now: self.ledger.add_watch(
            session, now, repo, branch=branch, why="opened"
        )

    def pr_merge(self, action):
        found = bash_actions.find_pr_url(self.text)
        repo, _ = self.repo(action, found[0] if found else None)
        number = action.get("number")
        branch = None if number else (action.get("branch") or self.branch(action["dir"]))
        if not repo or not (number or branch):
            return None
        why = "armed" if action.get("auto") else "merge"
        return lambda session, now: self.ledger.add_watch(
            session, now, repo, number=number, branch=branch, why=why
        )

    def release(self, action):
        if bash_actions.release_failed(self.text):
            return None
        found = bash_actions.find_release_url(self.text)  # (repo, tag) the release printed, or None
        tag = action.get("tag") or (found[1] if found else None)
        repo, where = self.repo(action, found[0] if found else None)
        draft = bool(action.get("draft"))
        return lambda session, now: self.ledger.record_event(
            session, now, "release", repo=repo, where=where, ref=tag, draft=draft
        )

    def write(self, action):
        paths = action.get("paths")
        if not paths:
            return None
        repo, where = self.ledger.locate(paths[0])
        return lambda session, now: self.ledger.record_edit(
            session, now, repo=repo, where=where, n=1
        )


def main():
    payload = _parse_stdin()
    if payload.get("tool_name") != "Bash":
        return
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    actions = bash_actions.parse(command, payload.get("cwd"))
    if not actions:
        return  # nearly every command: no ledger import, no state read, no subprocess
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return

    # Imported here, not at the top: the return above is the common exit of a hook that runs on
    # EVERY Bash call, and ledger pulls in state, subprocess and datetime (about 10 ms of imports
    # on a 20 ms process).
    import ledger as ledger_mod

    resolver = _Resolver(ledger_mod, bash_actions.response_text(payload.get("tool_response")))
    records = [r for r in (resolver.resolve(action) for action in actions) if r is not None]
    if not records:
        return  # a failed commit or release, a merge with nothing to watch: nothing happened

    # A subagent's payload may carry its own transcript; the one worth naming at /clear is the
    # primary's, which prompt_checkpoint.py and publish_reminder.py note.
    transcript = None if payload.get("agent_id") else payload.get("transcript_path")

    def apply(session):
        now = ledger_mod.utcnow()
        ledger_mod.note_transcript(session, transcript)
        for record in records:
            try:
                record(session, now)
            except Exception:
                pass  # one malformed record must not cost the others

    ledger_mod.mutate_session(sid, apply)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit, never output
    sys.exit(0)
