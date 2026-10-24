#!/usr/bin/env python3
"""Release-gate invariant tests for the gitian-kb nudge layer (0.33.0: checkpoints that re-arm).

Drives the REAL hook scripts end to end via `sh <hook>.sh` (never their python modules directly),
matching the idiom every other test_*.py file in this directory already uses: GITIAN_KB_STATE_FILE
(and HOME, defensively) point at a fresh tempdir per test, so runs never touch a real
~/.claude/gitian-kb/state.json and never interfere with each other or with a developer's real
session state. Nothing here can reach GitHub either: a stand-in `gh` is first on every hook's PATH
and fails, except for the one question the Stop checkpoint asks (a watched pull request's state),
which a test answers by writing the response file. The work happens in a throwaway checkout whose
`origin` is git@github.com:acme/widgets.git, so commits are REAL ones and every record carries a
repo.

Time never passes in here and nothing sleeps. The hooks read the real clock, so a test that needs
the user to come back 31 minutes or three hours later moves every timestamp the session has
recorded EARLIER (`pass_time`) -- exactly equivalent to that time having passed, since each hook
only compares what is recorded with "now".

Three scenarios (this file IS the release gate):

  A. CompliantSessionEmitsZeroNudges
     A compliant session -- orient, edit, commit, brief kb-scribe, the scribe's publish and append
     arriving with an `agent_id`, a follow-up to the live scribe, user prompts, turn ends, a resume
     -- must never emit a single nudge, from any hook, whether the install is warm (a vocabulary
     cache) or fresh. SessionStart's additionalContext is allowed (it is context, not a nudge): the
     assertion there is "valid JSON, not shaped like a PreToolUse deny or a Stop block". So is every
     other way of handing the work over: a scribe the agent waits for (its publishes land before the
     dispatch returns), a merge the session made itself, an armed auto-merge the agent saw land and
     briefed by number -- however late the Stop that polls for it -- and a delegated session, which
     is never spoken to at all. Each silence has a control: the same work without the brief IS
     nudged.

  B. EveryNudgeFiresOnlyOnNewActivityAtItsCheckpoint
     A nudge fires only on activity it has not shown, and only at its checkpoint. Orientation, the
     near-miss lint and the mint follow-up still latch once per epoch, and a clear re-arms them. A
     Stop blocks on a session's first work and never again for the same activity; a new merge
     blocks only once 30 minutes have passed; work alone never blocks, it arrives as context on the
     user's next prompt once two hours have; a scribe's note covers only what its brief held; a
     standing rule gets one hint, then the 10-minute floor; SessionStart names the unwritten
     activity on resume and clear. No PreToolUse NUDGE ever decides a permission (the routing guard
     is a precondition, not a nudge: it denies by design).

  C. FailOpenSweep
     Every hook script under plugins/gitian-kb/hooks/*.sh -- swept by glob, not a hardcoded list,
     so a hook added by a later task is automatically covered -- must exit 0 with empty-or-valid
     JSON stdout under: empty stdin, garbage stdin, valid stdin against a corrupt state file, valid
     stdin against an unwritable state directory, and valid stdin against a live session whose
     watched pull request cannot be polled because `gh` fails. A nudge hook must never crash a
     session, and none may reach a real `gh`.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_invariant.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
STATE_PY = HOOKS_DIR / "state.py"

SESSION_CONTEXT_SH = HOOKS_DIR / "session-context.sh"
HARVEST_SH = HOOKS_DIR / "harvest.sh"
ORIENTATION_SH = HOOKS_DIR / "orientation-check.sh"
PUBLISH_LINT_SH = HOOKS_DIR / "publish-lint.sh"
ROUTING_GUARD_SH = HOOKS_DIR / "routing-guard.sh"
BASH_ACTIVITY_SH = HOOKS_DIR / "bash-activity.sh"
BRIEF_WATCH_SH = HOOKS_DIR / "brief-watch.sh"
PROMPT_CHECKPOINT_SH = HOOKS_DIR / "prompt-checkpoint.sh"
PUBLISH_REMINDER_SH = HOOKS_DIR / "publish-reminder.sh"

# Every hook script that ships in this directory -- Scenario C sweeps this list (not a hardcoded
# one) so a hook added by a later task is automatically covered by the fail-open gate.
ALL_HOOK_SCRIPTS = sorted(HOOKS_DIR.glob("*.sh"))
# Sourced by the hooks, never registered with Claude Code: swept like the rest (it must not
# misbehave when run), but it has no stdin contract to give an envelope for.
HOOK_LIBRARIES = ("lib-state.sh",)

SID = "sess-1"
REPO = "acme/widgets"
REMOTE = "git@github.com:acme/widgets.git"
SERVER_KEY = "https://gitian.dev/api/mcp/kb"  # default GITIAN_KB_URL, per the state contract
KB = "mcp__plugin_gitian-kb_gitian-kb__"  # the plugin-scoped spelling of the KB server's tools
TRANSCRIPT = "/tmp/gks-invariant-transcript.jsonl"  # never opened, only recorded
SCRIBE_ID = "a1b2c3d4e5f6a7b8"
LIBRARIAN_ID = "b2c3d4e5f6a7b8c9"
# What a brief to the scribe says when the test does not care. It names no pull request.
BRIEF = "Record what happened on the widget loader in the journal."

RULE = "From now on, never merge with --auto on gitian."
OTHER_RULE = "Always run the migration test before a release."
THIRD_RULE = "Going forward, each org gets its own runner user."

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
STAMP_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")

# A test double for `gh`: it notes the shape of every call (its first two words) and fails, except
# for the one question the Stop checkpoint asks -- `gh api graphql` -- which it answers from
# $GKS_GH_RESPONSE when a test has written one. The only other thing a hook asks of `gh` is
# SessionStart's `gh pr view`, and a failing answer to that is simply "no open pull request".
FAKE_GH = r"""#!/bin/sh
printf '%s %s\n' "${1-}" "${2-}" >> "${GKS_GH_LOG:-/dev/null}"
if [ "${1-}" = "api" ] && [ "${2-}" = "graphql" ] && [ -f "${GKS_GH_RESPONSE-}" ]; then
  cat "$GKS_GH_RESPONSE"
  exit 0
fi
exit 1
"""
# The only ways any hook is allowed to call `gh` at all. A new one is a new way to reach GitHub from
# a hook and should be a deliberate decision, so it has to be added here.
SANCTIONED_GH_CALLS = ("api graphql", "pr view")


# --- envelope builders -- one per hook's stdin contract, mirroring each hook's own test file -----


def envelope(event, session_id, cwd, **fields):
    payload = {
        "session_id": session_id,
        "transcript_path": TRANSCRIPT,
        "cwd": cwd,
        "hook_event_name": event,
    }
    payload.update(fields)
    return payload


def session_start_envelope(source="startup", session_id=SID, cwd="/repo", **fields):
    return envelope("SessionStart", session_id, cwd, source=source, **fields)


def mcp_text(obj):
    """A KB tool's PostToolUse `tool_response` as Claude Code sends it: a BARE LIST of content
    blocks, the server's JSON as the text of one of them."""
    return [{"type": "text", "text": json.dumps(obj)}]


def harvest_envelope(
    tool_name, tool_input=None, tool_response=None, session_id=SID, agent_id=None, cwd="/repo"
):
    payload = envelope(
        "PostToolUse",
        session_id,
        cwd,
        tool_name=tool_name,
        tool_input=tool_input if tool_input is not None else {},
        tool_response=tool_response if tool_response is not None else {},
    )
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


def orientation_envelope(file_path="/x", session_id=SID, cwd="/repo"):
    return envelope(
        "PreToolUse", session_id, cwd, tool_name="Edit", tool_input={"file_path": file_path}
    )


def pre_tool_envelope(tool_name, tool_input=None, session_id=SID, agent_id=None, cwd="/repo"):
    """PreToolUse for a KB write -- what the publish lint and the routing guard are handed."""
    payload = envelope(
        "PreToolUse",
        session_id,
        cwd,
        tool_name=tool_name,
        tool_input=tool_input if tool_input is not None else {},
    )
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


def bash_envelope(command, stdout="", cwd="/repo", session_id=SID, agent_id=None):
    payload = envelope(
        "PostToolUse",
        session_id,
        cwd,
        tool_name="Bash",
        tool_input={"command": command},
        tool_response={"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False},
    )
    if agent_id:
        payload["agent_id"] = agent_id
    return payload


def dispatch_envelope(
    subagent_type, agent_id=None, session_id=SID, cwd="/repo", event="PostToolUse", prompt=BRIEF
):
    """An Agent dispatch in the background, at either end: PreToolUse as it starts (no response
    yet), PostToolUse as it returns with the dispatched agent's id."""
    payload = envelope(
        event,
        session_id,
        cwd,
        tool_name="Agent",
        tool_input={
            "subagent_type": subagent_type,
            "description": "KB work",
            "prompt": prompt,
            "run_in_background": True,
        },
    )
    if event == "PostToolUse":
        payload["tool_response"] = {"status": "async_launched", "agentId": agent_id}
    return payload


def message_envelope(to, session_id=SID, cwd="/repo", event="PostToolUse"):
    """A SendMessage to an agent that is already running, at either end of the call."""
    payload = envelope(
        event,
        session_id,
        cwd,
        tool_name="SendMessage",
        tool_input={"to": to, "message": "one more thing to record"},
    )
    if event == "PostToolUse":
        payload["tool_response"] = {"success": True}
    return payload


def prompt_envelope(prompt, session_id=SID, cwd="/repo", **fields):
    return envelope("UserPromptSubmit", session_id, cwd, prompt=prompt, **fields)


def stop_envelope(session_id=SID, stop_hook_active=False, cwd="/repo", **fields):
    return envelope("Stop", session_id, cwd, stop_hook_active=stop_hook_active, **fields)


def minted_response(slugs, slug="new-doc"):
    """Real MCP wire shape for the organic_topics_minted warning (see test_mint_followup.py):
    a LintWarning nested inside an MCP content text block."""
    return mcp_text(
        {
            "slug": slug,
            "warnings": [
                {
                    "code": "organic_topics_minted",
                    "path": "topics",
                    "note": "auto-minted as organic, live immediately: " + ", ".join(slugs),
                }
            ],
        }
    )


def hook_envelopes(sid, work):
    """Valid stdin envelopes for every hook script, keyed by file name: every shape the hook acts
    on, each built so its own guard passes and its state-touching logic actually runs -- the point
    of the sweep is to stress the read-modify-write path under a corrupt/unwritable state substrate
    and a failing `gh`, not just to exercise the early "not my call" guard."""
    return {
        "session-context.sh": [session_start_envelope("resume", sid, work)],
        "harvest.sh": [
            harvest_envelope(
                KB + "get", {"slug": "x"}, mcp_text({"slug": "x", "rev": 1}), sid, cwd=work
            ),
            # A content write by the scribe: the one that moves the ledger's checkpoint.
            harvest_envelope(
                KB + "append_entry",
                {"slug": "x", "repo": REPO},
                mcp_text({"slug": "x", "rev": 2}),
                sid,
                SCRIBE_ID,
                work,
            ),
        ],
        "orientation-check.sh": [orientation_envelope(os.path.join(work, "a.py"), sid, work)],
        "publish-lint.sh": [
            pre_tool_envelope(KB + "publish_doc", {"title": "x", "topics": ["x"]}, sid, cwd=work)
        ],
        # Neither `kb` nor `repo` inside a GitHub checkout: the one input on which it denies.
        "routing-guard.sh": [pre_tool_envelope(KB + "publish_doc", {"title": "x"}, sid, cwd=work)],
        "bash-activity.sh": [
            bash_envelope("git commit -m x", "[main abc1234] x\n 1 file changed\n", work, sid),
            bash_envelope("gh pr merge 650 --squash", "Merged pull request #650\n", work, sid),
        ],
        "brief-watch.sh": [
            dispatch_envelope("kb-scribe", None, sid, work, "PreToolUse"),
            dispatch_envelope("kb-scribe", SCRIBE_ID, sid, work, "PostToolUse"),
            message_envelope(SCRIBE_ID, sid, work, "PreToolUse"),
            message_envelope(SCRIBE_ID, sid, work, "PostToolUse"),
        ],
        "prompt-checkpoint.sh": [
            prompt_envelope(RULE, sid, work),
            prompt_envelope("ok", sid, work),
        ],
        "publish-reminder.sh": [stop_envelope(sid, False, work)],
    }


# --- time: move everything a session recorded EARLIER, which is the same as time passing --------


def earlier(node, delta):
    """`node` with every timestamp string anywhere inside it moved `delta` earlier."""
    if isinstance(node, str):
        if STAMP_RE.fullmatch(node):
            return (datetime.strptime(node, STAMP_FORMAT) - delta).strftime(STAMP_FORMAT)
        return node
    if isinstance(node, list):
        return [earlier(item, delta) for item in node]
    if isinstance(node, dict):
        return {key: earlier(value, delta) for key, value in node.items()}
    return node


def stamp_before(stamp, seconds):
    return earlier(stamp, timedelta(seconds=seconds))


def stamp_ago(seconds):
    """The real clock `seconds` ago, as the hooks write timestamps."""
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime(STAMP_FORMAT)


class InvariantTestCase(unittest.TestCase):
    """Shared plumbing for all three scenarios: fresh tempdir state per test, a real checkout to
    work in, a stand-in `gh`, subprocess helpers that drive the real `sh <hook>.sh` scripts, a small
    vocabulary of session actions built on them, and the generic silence/deny/fire/block assertions
    every hook's own test file already uses."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="gks-invariant-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.state_file = os.path.join(self.tmpdir, "nested", "state.json")
        self.bin_dir = os.path.join(self.tmpdir, "bin")
        os.makedirs(self.bin_dir)
        self.gh_log = os.path.join(self.tmpdir, "gh.log")
        self.gh_response = os.path.join(self.tmpdir, "gh-response.json")
        self.work = os.path.join(self.tmpdir, "work", "widgets")
        self.real_git = shutil.which("git") or "git"
        self.calls = []  # (hook file name, CompletedProcess) for every hook the test ran

        self.env = dict(os.environ)
        for name in (
            "GITIAN_KB_URL",
            "GITIAN_KB_MARKETPLACES_FILE",
            "CLAUDE_CONFIG_DIR",
            "GH_REPO",
            # A test run from inside a git hook inherits these, and every `git` below would then
            # read the hook's repository instead of the throwaway checkout.
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_PREFIX",
        ):
            self.env.pop(name, None)
        self.env.update(
            {
                "GITIAN_KB_STATE_FILE": self.state_file,
                # Defensive, per the task spec: even though every hook resolves its state path via
                # GITIAN_KB_STATE_FILE (always set above), point HOME at the tempdir too so nothing
                # in this suite could ever fall back to a developer's real ~/.claude/gitian-kb.
                "HOME": self.tmpdir,
                # SessionStart reads the checkout it runs in; without this it would read (and ask
                # `gh` about) whatever repository the test happens to be started from.
                "CLAUDE_PROJECT_DIR": self.work,
                # This host signs commits by default and the signer hangs: no global config at all.
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Test Author",
                "GIT_AUTHOR_EMAIL": "author@example.com",
                "GIT_COMMITTER_NAME": "Test Committer",
                "GIT_COMMITTER_EMAIL": "committer@example.com",
                "GKS_GH_LOG": self.gh_log,
                "GKS_GH_RESPONSE": self.gh_response,
                "PATH": self.bin_dir + os.pathsep + os.environ.get("PATH", ""),
            }
        )
        gh = os.path.join(self.bin_dir, "gh")
        with open(gh, "w", encoding="utf-8") as fh:
            fh.write(FAKE_GH)
        os.chmod(gh, 0o755)

        os.makedirs(self.work)
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", REMOTE)
        self.commit_file("Start the widgets repo")  # so HEAD exists before any hook asks about it

    # --- git: REAL commits, so the hooks read a real HEAD -----------------------------------------

    def git(self, *args):
        proc = subprocess.run(
            [self.real_git, "-C", self.work, "-c", "commit.gpgsign=false"] + list(args),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, msg="git %s: %s" % (" ".join(args), proc.stderr))
        return proc.stdout

    def commit_file(self, message):
        """Make a real commit; returns git's own output, which is what the Bash hook is handed."""
        with open(os.path.join(self.work, "widgets.txt"), "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
        self.git("add", "-A")
        return self.git("commit", "-m", message)

    # --- invocation -------------------------------------------------------------------------

    def run_hook(self, script, payload, env=None):
        input_text = payload if isinstance(payload, str) else json.dumps(payload)
        proc = subprocess.run(
            ["sh", str(script)],
            input=input_text,
            capture_output=True,
            text=True,
            env=env if env is not None else self.env,
            cwd=self.tmpdir,
            timeout=20,
        )
        self.calls.append((Path(script).name, proc))
        return proc

    def run_state(self, *args, input_text=None):
        return subprocess.run(
            [sys.executable, str(STATE_PY), *args],
            input=input_text,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=10,
        )

    def seed_vocab(self, topics):
        proc = self.run_state(
            "merge", input_text=json.dumps({"servers": {SERVER_KEY: {"topics": topics}}})
        )
        self.assertEqual(proc.returncode, 0)

    # --- what a session does, as the hooks see it ---------------------------------------------

    def session_start(self, sid, source="startup", **who):
        return self.run_hook(
            SESSION_CONTEXT_SH, session_start_envelope(source, sid, self.work, **who)
        )

    def prompt(self, sid, text, **who):
        return self.run_hook(PROMPT_CHECKPOINT_SH, prompt_envelope(text, sid, self.work, **who))

    def stop(self, sid, stop_hook_active=False, **who):
        return self.run_hook(
            PUBLISH_REMINDER_SH, stop_envelope(sid, stop_hook_active, self.work, **who)
        )

    def edit(self, sid, name="loader.py"):
        """The Edit tool, about to change a file in the checkout."""
        return self.run_hook(
            ORIENTATION_SH, orientation_envelope(os.path.join(self.work, name), sid, self.work)
        )

    def read(self, sid, agent_id=None):
        """A KB read (`search`): an orientation read, whoever made it."""
        return self.run_hook(
            HARVEST_SH,
            harvest_envelope(
                KB + "search", {"query": "widgets"}, mcp_text({"results": []}), sid, agent_id
            ),
        )

    def bash(self, sid, command, stdout="", agent_id=None):
        return self.run_hook(
            BASH_ACTIVITY_SH, bash_envelope(command, stdout, self.work, sid, agent_id)
        )

    def commit(self, sid, message, agent_id=None):
        """A REAL commit in the checkout, then the PostToolUse the Bash tool would have sent."""
        out = self.commit_file(message)
        return self.bash(sid, "git commit -m '%s'" % message, out, agent_id)

    def dispatch_started(self, sid, subagent_type, prompt=BRIEF):
        """The PreToolUse of a dispatch: the agent has just been told to start."""
        return self.run_hook(
            BRIEF_WATCH_SH,
            dispatch_envelope(subagent_type, None, sid, self.work, "PreToolUse", prompt),
        )

    def dispatch_returned(self, sid, subagent_type, agent_id, prompt=BRIEF):
        """The PostToolUse of a dispatch: it came back, carrying the dispatched agent's id."""
        return self.run_hook(
            BRIEF_WATCH_SH,
            dispatch_envelope(subagent_type, agent_id, sid, self.work, "PostToolUse", prompt),
        )

    def dispatch(self, sid, subagent_type, agent_id, prompt=BRIEF):
        """A background dispatch as Claude Code reports it, both ends of it. Returns both procs."""
        return [
            self.dispatch_started(sid, subagent_type, prompt),
            self.dispatch_returned(sid, subagent_type, agent_id, prompt),
        ]

    def message(self, sid, to):
        """A SendMessage to a running agent, both ends of it. Returns both procs."""
        return [
            self.run_hook(BRIEF_WATCH_SH, message_envelope(to, sid, self.work, event))
            for event in ("PreToolUse", "PostToolUse")
        ]

    def scribe_write(self, sid, tool, tool_input, result):
        """One KB write by the scribe, as the hooks see it: the PreToolUse publish lint, the
        PreToolUse routing guard, then the harvested success -- all carrying the scribe's `agent_id`
        under the parent's session id. Returns the three procs."""
        pre = pre_tool_envelope(KB + tool, tool_input, sid, SCRIBE_ID, self.work)
        post = harvest_envelope(KB + tool, tool_input, mcp_text(result), sid, SCRIBE_ID, self.work)
        return [
            self.run_hook(PUBLISH_LINT_SH, pre),
            self.run_hook(ROUTING_GUARD_SH, pre),
            self.run_hook(HARVEST_SH, post),
        ]

    def scribe_note(self, sid):
        """The scribe's journal append landing: a content write, so the KB now holds the brief."""
        return self.scribe_write(
            sid,
            "append_entry",
            {"slug": "journal-2026-10-24", "repo": REPO},
            {"slug": "journal-2026-10-24", "rev": 2},
        )

    def arm_merge(self, sid, number=651):
        """`gh pr merge --auto`: GitHub is told to merge once the checks pass. Only the command is
        ever seen; the merge itself, when it comes, is not."""
        self.assert_silent(
            self.bash(sid, "gh pr merge %d --auto --squash" % number, "Auto-merge enabled\n")
        )
        self.assertEqual(self.session(sid)["watch"][-1]["why"], "armed")

    def open_pull_request(self, sid, number=651):
        """`gh pr create`: a pull request this session opened, for someone else (or CI) to merge."""
        self.assert_silent(
            self.bash(
                sid,
                "gh pr create --title 'Ship the loader' --body 'The loader.'",
                "https://github.com/%s/pull/%d\n" % (REPO, number),
            )
        )
        self.assertEqual(self.session(sid)["watch"][-1]["why"], "opened")

    def set_pull_request(self, number, state, merged_at=None, title="Ship the loader"):
        """What the stand-in `gh api graphql` answers for the one watched pull request."""
        node = {"number": number, "state": state, "title": title, "mergedAt": merged_at}
        with open(self.gh_response, "w", encoding="utf-8") as fh:
            json.dump({"data": {"r0": {"p%d" % number: node}}}, fh)

    # --- state ------------------------------------------------------------------------------

    def state(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}

    def session(self, sid):
        return self.state().get("sessions", {}).get(sid, {})

    def ledger(self, sid):
        return self.session(sid).get("ledger", {})

    def merged_just_before_brief(self, sid):
        """GitHub's `mergedAt` for a merge that landed a moment before the latest brief."""
        return stamp_before(self.ledger(sid)["briefAt"], 1)

    def gh_calls(self):
        try:
            with open(self.gh_log, "r", encoding="utf-8") as fh:
                return fh.read().splitlines()
        except FileNotFoundError:
            return []

    def pass_time(self, minutes):
        """Let `minutes` go by: every timestamp the sessions have recorded moves that much earlier,
        so the next hook sees a clock that many minutes later."""
        delta = timedelta(minutes=minutes)
        state = self.state()
        if not state:
            return
        for session in state.get("sessions", {}).values():
            for key in ("ledger", "nudges", "watch", "updatedAt"):
                if key in session:
                    session[key] = earlier(session[key], delta)
            # A bucket's `min` is its grouping key -- the minute of its `at`, which just moved.
            for bucket in session.get("ledger", {}).get("edits", []):
                bucket["min"] = bucket["at"][:16]
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)

    # --- assertions -------------------------------------------------------------------------

    @staticmethod
    def hook_of(proc):
        return os.path.basename(proc.args[1])

    def assert_silent(self, procs):
        """Exit 0 and nothing on stdout, for one hook run or a list of them (a Pre/Post pair)."""
        for proc in procs if isinstance(procs, (list, tuple)) else [procs]:
            self.assertEqual(
                proc.returncode,
                0,
                msg="%s: stdout=%r stderr=%r" % (self.hook_of(proc), proc.stdout, proc.stderr),
            )
            self.assertEqual(proc.stdout, "", msg=self.hook_of(proc))

    def assert_context_not_a_nudge(self, proc):
        """SessionStart's additionalContext is allowed content, not a nudge -- valid JSON whose
        shape is neither a PreToolUse deny (`hookSpecificOutput.permissionDecision`) nor a Stop
        block (`decision`/`reason`). Checked structurally (parsed keys), not by substring search,
        so prose that happens to contain the word "decision" can never trip a false positive."""
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertNotIn("decision", payload)
        hso = payload.get("hookSpecificOutput")
        self.assertIsInstance(hso, dict)
        self.assertEqual(hso.get("hookEventName"), "SessionStart")
        self.assertNotIn("permissionDecision", hso)
        return payload

    def context_of(self, proc):
        """The additionalContext text of a SessionStart that is not a nudge."""
        return self.assert_context_not_a_nudge(proc)["hookSpecificOutput"]["additionalContext"]

    def assert_pre_tool_context(self, proc):
        """Advice that does not decide anything: PreToolUse additionalContext with no
        permissionDecision, so the call is neither blocked nor pre-approved."""
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertNotIn("permissionDecision", out)
        return out["additionalContext"]

    def assert_post_tool_context(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        return out["additionalContext"]

    def assert_prompt_context(self, proc):
        """Advice that rides on the user's own message: one JSON object holding nothing but a
        UserPromptSubmit additionalContext. A `decision` here would swallow the prompt."""
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(list(payload), ["hookSpecificOutput"])
        out = payload["hookSpecificOutput"]
        self.assertEqual(sorted(out), ["additionalContext", "hookEventName"])
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertTrue(out["additionalContext"])
        return out["additionalContext"]

    def assert_block(self, proc):
        self.assertEqual(proc.returncode, 0, msg="stderr=%r" % proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(sorted(payload), ["decision", "reason"])
        self.assertEqual(payload["decision"], "block")
        self.assertTrue(payload["reason"])
        return payload["reason"]

    def assert_fail_open(self, proc):
        """The Scenario C contract: exit 0, and stdout is either empty or one well-formed JSON
        object -- never a traceback, never a non-zero exit, never partial/malformed output."""
        self.assertEqual(
            proc.returncode,
            0,
            msg="%s: stdout=%r stderr=%r" % (self.hook_of(proc), proc.stdout, proc.stderr),
        )
        if proc.stdout.strip():
            json.loads(proc.stdout)

    def assert_nothing_nudged(self):
        """The release-gate invariant itself, restated as one blanket assertion over EVERY hook the
        test ran: exit 0, and nothing on stdout but SessionStart's allowed context."""
        self.assertTrue(self.calls)
        for name, proc in self.calls:
            self.assertEqual(proc.returncode, 0, msg=name)
            if name != "session-context.sh":
                self.assertEqual(proc.stdout, "", msg=name)


class CompliantSessionEmitsZeroNudges(InvariantTestCase):
    """Scenario A -- the release gate itself: a session that does everything right (orients before
    editing, briefs the scribe for what it did, lets the scribe's note land) must never see a single
    nudge. Every nudge is evaluated at a checkpoint (turn end, the user's next prompt, a restart),
    by which time a compliant agent has already handed the work over."""

    def compliant_session(self, sid, warm):
        # 1. SessionStart(startup) -- context is allowed content, not a nudge.
        self.assert_context_not_a_nudge(self.session_start(sid))
        if not warm:
            self.assertFalse(os.path.exists(self.state_file))  # nothing has touched state yet

        # 2. The user's first message of the session.
        self.assert_silent(self.prompt(sid, "add the widget loader"))

        # 3. Orient. A librarian dispatch is not a brief -- only a scribe is -- and its reads land
        # under the parent's session id, which is what satisfies the orientation check below.
        self.assert_silent(self.dispatch(sid, "kb-librarian", LIBRARIAN_ID))
        self.assert_silent(self.read(sid, agent_id=LIBRARIAN_ID))
        reads = 1
        if warm:
            vocab_topics = [
                {"slug": "widgets", "description": "The widget library", "degree": 5},
                {"slug": "loader", "description": "Loading widgets", "degree": 3},
            ]
            vocab = harvest_envelope(
                "ReadMcpResourceTool",
                {"uri": "gitian-kb://vocab"},
                {
                    "contents": [
                        {"uri": "gitian-kb://vocab", "text": json.dumps({"topics": vocab_topics})}
                    ]
                },
                sid,
            )
            self.assert_silent(self.run_hook(HARVEST_SH, vocab))
            reads = 2
        self.assertEqual(self.session(sid)["gitianReads"], reads)
        self.assertNotIn("briefAt", self.ledger(sid))
        if not warm:
            self.assertEqual(self.state().get("servers", {}), {})  # vocab cache never populated

        # 4. Work: three edits (the threshold that would otherwise make them a nudge) and a real
        # commit, recorded through the Edit and Bash hooks.
        for name in ("loader.py", "loader_test.py", "README.md"):
            self.assert_silent(self.edit(sid, name))
        self.assert_silent(self.commit(sid, "Add the widget loader"))
        led = self.ledger(sid)
        self.assertEqual([e["kind"] for e in led["events"]], ["commit"])
        self.assertEqual(sum(b["n"] for b in led["edits"]), 3)
        self.assertEqual({e["repo"] for e in led["events"]}, {REPO})

        # 5. Brief kb-scribe (background) and end the turn: the brief is a pending cover, so the
        # Stop hook has nothing to say while the scribe is still writing -- and neither does a
        # session that is resumed in the meantime.
        self.assert_silent(self.dispatch(sid, "kb-scribe", SCRIBE_ID))
        self.assertEqual(self.ledger(sid)["scribeIds"], [SCRIBE_ID])
        self.assert_silent(self.stop(sid))
        self.assertNotIn("no KB write covers", self.context_of(self.session_start(sid, "resume")))

        # 6. The user reads the reply and types. The scribe's publish and append arrive with its
        # `agent_id`; with the topics real (warm) or the cache empty (fresh), the lint stays quiet.
        self.pass_time(2)
        first_brief = self.ledger(sid)["briefAt"]
        self.assert_silent(self.prompt(sid, "thanks, now the tests"))
        topics = ["widgets", "loader"] if warm else ["widget-loader"]
        self.assert_silent(
            self.scribe_write(
                sid,
                "publish_doc",
                {"title": "Widget loader", "repo": REPO, "topics": topics},
                {"slug": "widget-loader", "rev": 1},
            )
        )
        self.assert_silent(
            self.scribe_write(
                sid,
                "append_entry",
                {"slug": "journal-2026-10-24", "repo": REPO, "topics": topics},
                {"slug": "journal-2026-10-24", "rev": 4},
            )
        )
        led = self.ledger(sid)
        self.assertEqual((led["events"], led["edits"]), ([], []))  # the brief's work is covered
        self.assertEqual(led["checkpointAt"], first_brief)

        # 7. The session keeps working. A commit made after the checkpoint is open, but work alone
        # never blocks a turn end; a follow-up to the LIVE scribe (single-flight) covers it.
        self.pass_time(2)
        self.assert_silent(self.commit(sid, "Retry the loader on a timeout"))
        self.assertEqual(len(self.ledger(sid)["events"]), 1)
        self.assert_silent(self.stop(sid))
        before = self.ledger(sid)["briefAt"]
        self.assert_silent(self.message(sid, SCRIBE_ID))
        self.assertGreater(self.ledger(sid)["briefAt"], before)
        self.assert_silent(self.stop(sid))
        self.assert_silent(
            self.scribe_write(
                sid,
                "append_entry",
                {"slug": "journal-2026-10-24", "repo": REPO, "topics": topics},
                {"slug": "journal-2026-10-24", "rev": 5},
            )
        )
        led = self.ledger(sid)
        self.assertEqual((led["events"], led["edits"]), ([], []))

        # 8. More turns, and the user comes back half an hour later: still nothing, including on
        # a resume, which lists only what no KB write covers.
        self.assert_silent(self.prompt(sid, "ship it"))
        self.assert_silent(self.stop(sid))
        self.pass_time(35)
        self.assert_silent(self.prompt(sid, "back again"))
        self.assert_silent(self.stop(sid))
        resume = self.context_of(self.session_start(sid, "resume"))
        self.assertNotIn("no KB write covers", resume)

        # Every nudge-capable invocation above produced empty stdout, exit 0 -- the release-gate
        # invariant itself, restated as one final blanket assertion. And a session with nothing
        # watched never asks GitHub anything beyond SessionStart's open-PR question.
        self.assert_nothing_nudged()
        self.assertLessEqual(set(self.gh_calls()), {"pr view"})

    def test_fully_compliant_session_produces_no_nudge_output_anywhere(self):
        self.compliant_session("sess-compliant", warm=True)

    def test_fresh_install_compliant_session_produces_no_nudge_output_anywhere(self):
        """Variant of the above with an EMPTY/ABSENT state cache from the very start -- no prior
        `gitian-kb://vocab` resource read has EVER seeded `servers.<key>.topics` (a first-ever
        session against a brand-new plugin install, before `kb-librarian` or anything else has
        ever populated the local vocab cache). Publishing with topics the empty cache doesn't
        know about must still be completely silent: the lint's one rule (near-miss) is explicitly
        gated off when the cache is empty (see publish_lint.py's `_check_near_miss`)."""
        self.compliant_session("sess-fresh-install", warm=False)

    def test_the_brief_is_what_keeps_a_session_quiet(self):
        """The control for the two tests above: without it, silence could mean the hooks record
        nothing. The same orientation, edits and commit, ending the same turn -- the only
        difference is whether the scribe was briefed."""
        for briefed in (True, False):
            with self.subTest(briefed=briefed):
                sid = "sess-briefed" if briefed else "sess-forgot"
                self.assert_silent(self.read(sid))
                for name in ("loader.py", "loader_test.py", "README.md"):
                    self.assert_silent(self.edit(sid, name))
                self.assert_silent(self.commit(sid, "Add the widget loader"))
                if briefed:
                    self.assert_silent(self.dispatch(sid, "kb-scribe", SCRIBE_ID))
                    self.assert_silent(self.stop(sid))
                else:
                    reason = self.assert_block(self.stop(sid))
                    self.assertIn(REPO, reason)
                    self.assertIn("1 commit", reason)
                    self.assertIn("3 file edits", reason)

    def test_a_foreground_scribe_is_covered_from_the_moment_its_dispatch_starts(self):
        """A scribe the agent waits for publishes while its dispatch is still running -- before the
        PostToolUse that returns its id. So the brief has to be recorded when the dispatch STARTS
        (PreToolUse), or that publish confirms nothing: it would be pinned to ten minutes ago, and
        the work the agent has just handed over would still be open."""
        sid = "sess-foreground"
        self.assert_silent(self.read(sid))
        for name in ("loader.py", "loader_test.py", "README.md"):
            self.assert_silent(self.edit(sid, name))
        self.assert_silent(self.commit(sid, "Add the widget loader"))

        self.assert_silent(self.dispatch_started(sid, "kb-scribe"))
        self.assert_silent(
            self.scribe_write(
                sid,
                "publish_doc",
                {"title": "Widget loader", "repo": REPO, "topics": ["widget-loader"]},
                {"slug": "widget-loader", "rev": 1},
            )
        )
        led = self.ledger(sid)
        self.assertEqual((led["events"], led["edits"]), ([], []))
        self.assertEqual(led["checkpointAt"], led["briefAt"])

        # ...and only now does the dispatch return, with the scribe's id.
        self.assert_silent(self.dispatch_returned(sid, "kb-scribe", SCRIBE_ID))
        self.assertEqual(self.ledger(sid)["scribeIds"], [SCRIBE_ID])
        self.assert_silent(self.stop(sid))
        self.pass_time(125)
        self.assert_silent(self.prompt(sid, "what did we land?"))
        self.assert_silent(self.stop(sid))
        self.assert_nothing_nudged()

    def merge_and_brief(self, sid, number=650):
        """The compliant order for a merge the session makes ITSELF: a plain `gh pr merge`, then a
        brief to kb-scribe right after (about the loader, naming no pull request). Recording the
        command never asks GitHub; the pull request is only watched."""
        merge = self.bash(
            sid,
            "gh pr merge %d --squash" % number,
            "\u2713 Squashed and merged pull request #%d (Ship the loader)\n" % number,
        )
        self.assert_silent(merge)
        self.assertEqual(self.gh_calls(), [])
        watch = self.session(sid)["watch"]
        self.assertEqual(
            [(w["repo"], w["number"], w["why"]) for w in watch], [(REPO, number, "merge")]
        )
        self.assert_silent(self.dispatch(sid, "kb-scribe", SCRIBE_ID))

    def test_a_merge_the_session_made_and_briefed_is_covered_by_the_brief_and_then_the_note(self):
        """The poll finds MERGED right after the brief: the merge is stamped with GitHub's mergedAt
        (the agent knew about it when it happened), so the pending brief covers it. It must STAY
        covered once the scribe's note lands and the brief's 30-minute grace is long over -- if the
        merge were stamped when the poll saw it instead, it would sit past the checkpoint and block
        the first turn end after the floor."""
        sid = "sess-merge"
        self.merge_and_brief(sid)
        merged_at = self.merged_just_before_brief(sid)
        self.set_pull_request(650, "MERGED", merged_at)

        self.assert_silent(self.stop(sid))
        self.assertEqual(self.gh_calls(), ["api graphql"])
        led = self.ledger(sid)
        self.assertEqual(
            [(e["kind"], e["ref"], e["at"]) for e in led["events"]], [("merge", "#650", merged_at)]
        )
        self.assertEqual(self.session(sid)["watch"], [])

        self.assert_silent(self.scribe_note(sid))
        self.assertEqual(self.ledger(sid)["events"], [])
        self.pass_time(31)
        self.assert_silent(self.stop(sid))
        self.assert_silent(self.prompt(sid, "welcome back"))
        self.assert_nothing_nudged()
        self.assertEqual(self.gh_calls(), ["api graphql"])  # one poll, once the merge was seen

    def test_a_merge_polled_late_is_covered_just_the_same(self):
        """The first Stop that polls comes 31 minutes after the brief and the scribe's note: the
        merge is still the brief's, not news."""
        sid = "sess-merge-late"
        self.merge_and_brief(sid)
        self.assert_silent(self.scribe_note(sid))
        self.pass_time(31)
        self.set_pull_request(650, "MERGED", self.merged_just_before_brief(sid))

        self.assert_silent(self.stop(sid))
        self.assertEqual(self.gh_calls(), ["api graphql"])
        self.assertEqual(self.session(sid)["watch"], [])
        self.assert_silent(self.prompt(sid, "welcome back"))
        self.assert_nothing_nudged()

    def test_the_same_merge_unbriefed_is_nudged(self):
        """The control for the merge tests: without the brief, the poll that finds MERGED is the
        session's first checkpoint with something to say."""
        sid = "sess-merge-forgot"
        self.assert_silent(self.bash(sid, "gh pr merge 650 --squash", "Merged pull request #650\n"))
        self.set_pull_request(650, "MERGED", stamp_ago(5))
        reason = self.assert_block(self.stop(sid))
        self.assertIn("#650", reason)
        self.assertIn(REPO, reason)
        self.assertIn("kb-scribe", reason)

    def test_a_merge_the_agent_saw_land_and_briefed_by_name_is_covered(self):
        """`gh pr merge --auto` only ARMS a merge, and a pull request the session OPENED is merged
        by someone else; GitHub lands either later, unseen by any hook. When the agent notices it
        land and writes it up -- naming the pull request in the brief -- that brief covers it,
        though the poll only finds it afterwards: the merge keeps GitHub's mergedAt instead of the
        poll's own time. Compliant, so silent, now and 31 minutes on -- whether the brief names the
        pull request as `#N` or by its URL."""
        by_number = "Pull request #651 merged: record the rollout."
        by_url = "Record the rollout of https://github.com/acme/widgets/pull/651, now merged."
        cases = {
            "armed, named by number": (self.arm_merge, by_number),
            "armed, named by url": (self.arm_merge, by_url),
            "opened, named by number": (self.open_pull_request, by_number),
        }
        for label, (start, brief) in cases.items():
            with self.subTest(case=label):
                sid = "sess-named-" + label.replace(", ", "-").replace(" ", "-")
                start(sid)
                self.assert_silent(self.dispatch(sid, "kb-scribe", SCRIBE_ID, prompt=brief))
                merged_at = self.merged_just_before_brief(sid)
                self.set_pull_request(651, "MERGED", merged_at)
                self.assert_silent(self.stop(sid))
                merges = [e for e in self.ledger(sid)["events"] if e["kind"] == "merge"]
                self.assertEqual([(e["ref"], e["at"]) for e in merges], [("#651", merged_at)])

                self.assert_silent(self.scribe_note(sid))
                self.pass_time(31)
                self.assert_silent(self.stop(sid))
                self.assert_silent(self.prompt(sid, "welcome back"))
        self.assert_nothing_nudged()

    def test_an_armed_merge_nobody_wrote_up_surfaces_once_the_floor_passes(self):
        """The control for the armed merge: the same auto-merge landing while the agent briefed the
        scribe about something ELSE is the incident this layer exists for. The brief cannot have
        held it, so it is stamped when the poll saw it -- after the brief and the scribe's note --
        and the first turn end past the 30-minute floor says so."""
        sid = "sess-armed-unseen"
        self.arm_merge(sid)
        self.assert_silent(self.dispatch(sid, "kb-scribe", SCRIBE_ID))  # names no pull request
        self.pass_time(1)
        self.set_pull_request(651, "MERGED", self.merged_just_before_brief(sid))

        self.assert_silent(self.stop(sid))  # news, but the scribe was briefed a minute ago
        self.assert_silent(self.scribe_note(sid))
        self.pass_time(31)
        reason = self.assert_block(self.stop(sid))
        self.assertIn("#651", reason)

    def test_a_delegated_session_is_never_nudged_and_spends_nothing_of_the_primarys(self):
        """A subagent (`agent_id`) or a `claude --agent <name>` session (`agent_type`) has no tool
        to dispatch or brief a scribe with, so no checkpoint may speak to it -- not even with the
        first work of its session waiting, which blocks the primary. And its silence is not the
        primary's nudge spent: the primary's own turn end still blocks afterwards."""
        sid = "sess-delegated"
        self.assert_silent(self.read(sid))
        for name in ("loader.py", "loader_test.py", "README.md"):
            self.assert_silent(self.edit(sid, name))
        self.assert_silent(self.commit(sid, "Add the widget loader"))

        for who in ({"agent_id": SCRIBE_ID}, {"agent_type": "kb-scribe"}):
            with self.subTest(**who):
                self.assert_silent(self.stop(sid, **who))
                self.assert_silent(self.prompt(sid, RULE, **who))
                resume = self.context_of(self.session_start(sid, "resume", **who))
                self.assertNotIn("no KB write covers", resume)

        reason = self.assert_block(self.stop(sid))
        self.assertIn("1 commit", reason)
        self.assertNotIn("the user:", reason)  # the delegated session's "rule" was never recorded


class EveryNudgeFiresOnlyOnNewActivityAtItsCheckpoint(InvariantTestCase):
    """Scenario B -- a nudge fires only on activity it has not already shown, and only at its
    checkpoint: the Stop block for completion points, the user's next prompt for everything else,
    SessionStart for a session that restarts. Time alone re-arms nothing."""

    def first_work_block(self, sid):
        """A session's first checkpoint with work nobody wrote down: orientation done, three edits
        and a real commit, no brief. The Stop blocks -- the one block this layer is for."""
        self.assert_silent(self.read(sid))
        for name in ("loader.py", "loader_test.py", "README.md"):
            self.assert_silent(self.edit(sid, name))
        self.assert_silent(self.commit(sid, "Add the widget loader"))
        return self.assert_block(self.stop(sid))

    def test_orientation_lint_and_mint_latch_once_per_epoch_and_a_clear_re_arms_them(self):
        sid = "sess-latch"

        # -- orientation: zero reads -> advice once, never a deny, repeat silent ----------------
        edit_payload = orientation_envelope(os.path.join(self.work, "a.py"), sid, self.work)
        first_orientation = self.run_hook(ORIENTATION_SH, edit_payload)
        advice = self.assert_pre_tool_context(first_orientation)
        self.assertIn("file_intents", advice)

        second_orientation = self.run_hook(ORIENTATION_SH, edit_payload)
        self.assert_silent(second_orientation)

        # -- publish lint (near-miss): advice once, never a deny, repeat silent ------------------
        self.seed_vocab(
            [
                {"slug": "auth", "description": "Authentication flows", "degree": 5},
                {"slug": "kb-discipline", "description": "KB discipline", "degree": 4},
            ]
        )
        near_miss_payload = pre_tool_envelope(
            KB + "publish_doc", {"title": "y", "topics": ["kb-disciplne"]}, sid, cwd=self.work
        )
        near_miss = self.run_hook(PUBLISH_LINT_SH, near_miss_payload)
        near_miss_context = self.assert_pre_tool_context(near_miss)
        self.assertIn('did you mean "kb-discipline"?', near_miss_context)

        second_near_miss = self.run_hook(PUBLISH_LINT_SH, near_miss_payload)
        self.assert_silent(second_near_miss)

        # -- mint follow-up: fires once per new slug, repeat envelope silent ---------------------
        minted_payload = harvest_envelope(
            KB + "publish_doc", tool_response=minted_response(["auth-flow"]), session_id=sid
        )
        first_mint = self.run_hook(HARVEST_SH, minted_payload)
        mint_context = self.assert_post_tool_context(first_mint)
        self.assertIn("auth-flow", mint_context)

        second_mint = self.run_hook(HARVEST_SH, minted_payload)
        self.assert_silent(second_mint)

        # -- a clear (SessionStart source=clear bumps the epoch) re-arms the once-per-epoch nudges
        self.assert_context_not_a_nudge(self.session_start(sid, "clear"))

        third_orientation = self.run_hook(ORIENTATION_SH, edit_payload)
        self.assert_pre_tool_context(third_orientation)
        self.assertIn(
            'did you mean "kb-discipline"?',
            self.assert_pre_tool_context(self.run_hook(PUBLISH_LINT_SH, near_miss_payload)),
        )
        self.assertIn(
            "auth-flow", self.assert_post_tool_context(self.run_hook(HARVEST_SH, minted_payload))
        )

    def test_the_stop_blocks_on_first_work_and_not_again_for_the_same_activity(self):
        sid = "sess-first-work"
        reason = self.first_work_block(sid)
        self.assertIn("gitian-kb checkpoint: activity since", reason)
        self.assertIn(REPO, reason)
        self.assertIn("1 commit", reason)
        self.assertIn("3 file edits", reason)
        self.assertIn("kb-scribe", reason)

        # The same activity is not news a second time: not at the next turn end, not on the user's
        # next prompt, not hours later.
        self.assert_silent(self.stop(sid))
        self.assert_silent(self.prompt(sid, "carry on"))
        self.pass_time(300)
        self.assert_silent(self.stop(sid))
        self.assert_silent(self.prompt(sid, "still there?"))

    def test_a_new_merge_blocks_only_past_the_completion_floor(self):
        sid = "sess-merge-floor"
        self.first_work_block(sid)
        self.assertEqual(self.gh_calls(), [])  # nothing watched yet: a turn end asks nobody

        # Auto-merge is ARMED by the command; the merge itself happens on GitHub, unseen, and only
        # the Stop poll can find it. Five minutes after the last block it is news, but not yet
        # worth a turn.
        self.pass_time(5)
        self.arm_merge(sid)
        self.set_pull_request(651, "MERGED", stamp_ago(5))
        self.assert_silent(self.stop(sid))
        self.assertEqual(self.gh_calls(), ["api graphql"])
        merges = [e["ref"] for e in self.ledger(sid)["events"] if e["kind"] == "merge"]
        self.assertEqual(merges, ["#651"])  # learned, remembered, not yet shown
        self.assertEqual(self.session(sid)["watch"], [])

        # Past the 30-minute floor (31 minutes after the block): once.
        self.pass_time(26)
        reason = self.assert_block(self.stop(sid))
        self.assertIn("#651", reason)
        self.assert_silent(self.stop(sid))
        self.assertEqual(self.gh_calls(), ["api graphql"])  # the merged entry is not asked about

    def test_work_alone_never_blocks_and_arrives_as_prompt_context_after_two_hours(self):
        sid = "sess-work-only"
        self.first_work_block(sid)

        # An hour on, a new commit: the turn end stays silent (a block is a whole extra turn), and
        # so does the user's next prompt -- two hours have not passed since the last nudge.
        self.pass_time(60)
        self.assert_silent(self.commit(sid, "Retry the loader on a timeout"))
        self.assert_silent(self.stop(sid))
        self.assert_silent(self.prompt(sid, "what next?"))

        # 121 minutes after the block a turn end is STILL silent -- work never costs a turn, however
        # late -- but the user's next message carries it: context, never a block, listing what is
        # still unwritten, the new commit included.
        self.pass_time(61)
        self.assert_silent(self.stop(sid))
        context = self.assert_prompt_context(self.prompt(sid, "and now?"))
        self.assertTrue(context.startswith("gitian-kb checkpoint: activity since "))
        self.assertIn(REPO, context)
        self.assertIn("Retry the loader on a timeout", context)
        self.assertIn("kb-scribe", context)

        # ...once. Marked nudged: nothing new, nothing said.
        self.assert_silent(self.prompt(sid, "and after that?"))
        self.assert_silent(self.stop(sid))

    def test_a_scribes_note_covers_only_what_its_brief_held(self):
        """A scribe publishes minutes after its brief while the primary keeps working. Its note
        covers what the brief could have held and nothing newer -- if it covered everything up to
        its own landing, a commit made in between would be wiped from the ledger unwritten. It
        stays open, and surfaces on the user's message once two hours have passed."""
        sid = "sess-brief-bound"
        self.assert_silent(self.read(sid))
        self.assert_silent(self.commit(sid, "Add the widget loader"))  # in the brief
        self.assert_silent(self.dispatch(sid, "kb-scribe", SCRIBE_ID))
        self.pass_time(1)
        self.assert_silent(self.commit(sid, "Retry the loader on a timeout"))  # while it wrote
        self.assert_silent(self.scribe_note(sid))
        subjects = [e["text"] for e in self.ledger(sid)["events"]]
        self.assertEqual(subjects, ["Retry the loader on a timeout"])

        self.pass_time(121)
        context = self.assert_prompt_context(self.prompt(sid, "what next?"))
        self.assertIn("Retry the loader on a timeout", context)
        self.assertNotIn("Add the widget loader", context)

    def test_a_standing_rule_gets_one_hint_then_the_floor_and_never_costs_a_turn(self):
        sid = "sess-rule"
        context = self.assert_prompt_context(self.prompt(sid, RULE))
        self.assertIn("standing rule", context)
        self.assertIn(RULE, context)
        self.assertIn("kb-scribe", context)
        self.assertEqual(context.count("\n"), 0)  # one line: every hint is context paid for

        # A rule alone is not activity: the turn end stays silent however many rules were stated.
        self.assert_silent(self.stop(sid))
        # A second rule right behind it is recorded for the checkpoints to list, but not hinted.
        self.assert_silent(self.prompt(sid, OTHER_RULE))
        self.assert_silent(self.stop(sid))
        # Past the 10-minute floor the next one is hinted again.
        self.pass_time(11)
        context = self.assert_prompt_context(self.prompt(sid, THIRD_RULE))
        self.assertIn("standing rule", context)
        self.assertIn(THIRD_RULE, context)
        self.assert_silent(self.stop(sid))

    def test_session_start_names_unwritten_activity_on_resume_and_clear_never_on_startup(self):
        sid = "sess-restart"
        self.assert_silent(self.read(sid))
        for name in ("loader.py", "loader_test.py", "README.md"):
            self.assert_silent(self.edit(sid, name))
        self.assert_silent(self.commit(sid, "Add the widget loader"))

        startup = self.context_of(self.session_start(sid, "startup"))
        self.assertNotIn("no KB write covers", startup)

        resume = self.context_of(self.session_start(sid, "resume"))
        self.assertIn("resumed with activity since", resume)
        self.assertIn(REPO, resume)
        self.assertIn("1 commit", resume)
        self.assertIn("3 file edits", resume)
        self.assertIn("brief kb-scribe", resume)

        # /clear is the last place the work of a conversation it empties can be named: with the
        # transcript to brief from, once -- the ledger then starts over.
        clear = self.context_of(self.session_start(sid, "clear"))
        self.assertIn("before /clear, this session left activity since", clear)
        self.assertIn(REPO, clear)
        self.assertIn("with the transcript %s." % TRANSCRIPT, clear)
        again = self.context_of(self.session_start(sid, "clear"))
        self.assertNotIn("no KB write covers", again)
        self.assertNotIn(TRANSCRIPT, again)

    def test_no_pre_tool_nudge_ever_decides_a_permission(self):
        """The two PreToolUse NUDGES -- the orientation check and the publish lint -- only add
        context, on every path that makes them speak. (The routing guard is not a nudge: it is a
        precondition on the call, and denies by design.)"""
        sid = "sess-no-decision"
        orientation = self.run_hook(
            ORIENTATION_SH, orientation_envelope(os.path.join(self.work, "a.py"), sid, self.work)
        )
        self.seed_vocab([{"slug": "kb-discipline", "description": "KB discipline", "degree": 4}])
        lint = self.run_hook(
            PUBLISH_LINT_SH,
            pre_tool_envelope(
                KB + "publish_doc", {"title": "y", "topics": ["kb-disciplne"]}, sid, cwd=self.work
            ),
        )
        for proc in (orientation, lint):
            self.assertTrue(proc.stdout, msg="expected the nudge to speak: " + self.hook_of(proc))
            self.assert_pre_tool_context(proc)
            self.assertNotIn("permissionDecision", proc.stdout)


class FailOpenSweep(InvariantTestCase):
    """Scenario C -- every hook script in this directory must fail open: empty stdin, garbage
    stdin, valid stdin against a corrupt state file, valid stdin against an unwritable state
    directory, and valid stdin against a live session whose pull request poll fails must all exit 0
    with empty-or-valid-JSON stdout. Sweeps `hooks/*.sh` itself rather than a hardcoded list, so a
    hook added by a later task is automatically covered -- and a failing `gh` is first on PATH for
    the whole sweep, so no hook can ever reach a real one."""

    def valid_envelopes_for(self, script_name, sid):
        # Defensive fallback for a hook this test doesn't know the shape of yet (added by a later
        # task) -- still a plausible generic envelope, so the sweep at least exercises "does this
        # new hook crash", even without knowing its exact guard conditions.
        generic = [{"session_id": sid, "transcript_path": TRANSCRIPT, "cwd": self.work}]
        return hook_envelopes(sid, self.work).get(script_name, generic)

    def test_the_sweep_reaches_every_hook_of_this_release(self):
        names = {script.name for script in ALL_HOOK_SCRIPTS}
        shipped = {
            "session-context.sh",
            "prompt-checkpoint.sh",
            "orientation-check.sh",
            "publish-lint.sh",
            "routing-guard.sh",
            "harvest.sh",
            "bash-activity.sh",
            "brief-watch.sh",
            "publish-reminder.sh",
        }
        self.assertEqual(shipped - names, set(), msg="the glob misses hooks of this release")
        # Each of them has envelopes built for it, rather than the generic fallback.
        envelopes = hook_envelopes(SID, self.work)
        for name in sorted(names - set(HOOK_LIBRARIES)):
            self.assertIn(name, envelopes, msg="give %s valid envelopes in hook_envelopes" % name)

    def test_every_hook_fails_open_on_every_input_failure_mode(self):
        self.assertTrue(ALL_HOOK_SCRIPTS, "expected at least one hooks/*.sh script to sweep")

        for script in ALL_HOOK_SCRIPTS:
            name = script.name

            with self.subTest(script=name, case="empty_stdin"):
                proc = self.run_hook(script, "")
                self.assert_fail_open(proc)

            with self.subTest(script=name, case="garbage_stdin"):
                proc = self.run_hook(script, "not json")
                self.assert_fail_open(proc)

            with self.subTest(script=name, case="corrupt_state_file"):
                with tempfile.TemporaryDirectory(prefix="gks-invariant-corrupt-") as tmp:
                    state_file = os.path.join(tmp, "state.json")
                    env = dict(self.env)
                    env["GITIAN_KB_STATE_FILE"] = state_file
                    env["HOME"] = tmp
                    for payload in self.valid_envelopes_for(name, "sess-fo-corrupt"):
                        # Corrupt again before each run: a hook that writes rebuilds the file.
                        with open(state_file, "w", encoding="utf-8") as fh:
                            fh.write("{{{")
                        self.assert_fail_open(self.run_hook(script, payload, env=env))

            with self.subTest(script=name, case="unwritable_state_dir"):
                with tempfile.TemporaryDirectory(prefix="gks-invariant-unwritable-") as tmp:
                    # A regular file occupies the path component the state dir needs to become --
                    # os.makedirs() inside state.py can never create it (same technique as
                    # test_orientation.py's own CorruptStateFailOpen fixture).
                    blocker = os.path.join(tmp, "blocker")
                    with open(blocker, "w", encoding="utf-8") as fh:
                        fh.write("not a directory")
                    env = dict(self.env)
                    env["GITIAN_KB_STATE_FILE"] = os.path.join(blocker, "nested", "state.json")
                    env["HOME"] = tmp
                    for payload in self.valid_envelopes_for(name, "sess-fo-unwritable"):
                        self.assert_fail_open(self.run_hook(script, payload, env=env))

        # The hooks that talk to GitHub need a live session to do it: unwritten work, and a merge
        # to poll for. Every hook runs against one, with a `gh` that fails.
        sid = "sess-fo-live"
        self.assert_silent(self.read(sid))
        for name in ("loader.py", "loader_test.py", "README.md"):
            self.assert_silent(self.edit(sid, name))
        self.assert_silent(self.commit(sid, "Add the widget loader"))
        self.assert_silent(self.bash(sid, "gh pr merge 650 --squash", "Merged pull request #650\n"))
        self.assertTrue(self.session(sid)["watch"])
        for script in ALL_HOOK_SCRIPTS:
            with self.subTest(script=script.name, case="live_session_failing_gh"):
                for payload in self.valid_envelopes_for(script.name, sid):
                    self.assert_fail_open(self.run_hook(script, payload))

        # Nothing in the sweep reached a real `gh`: every call landed on the stand-in, and only in
        # the two shapes a hook may use -- and both of those hooks DID go through it (SessionStart
        # only asks when a `timeout` binary exists to bound the question).
        calls = self.gh_calls()
        for call in calls:
            self.assertIn(call, SANCTIONED_GH_CALLS)
        self.assertIn("api graphql", calls)
        if shutil.which("timeout") or shutil.which("gtimeout"):
            self.assertIn("pr view", calls)


if __name__ == "__main__":
    unittest.main()
