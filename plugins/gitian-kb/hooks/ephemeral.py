#!/usr/bin/env python3
"""ephemeral.py -- where a write is NOT the user's work: the one list every edit-counting hook reads.

The activity ledger (ledger.py) counts file edits so that a checkpoint can say what no KB write
covers yet, and three of them are enough to fire one. A file written into scratch space or into
Claude Code's own state is not that kind of work: nothing there owes a KB write and no in-flight
plan can claim the path, so counting it made a checkpoint out of a session that only drafted a
temp file, naming `/tmp (no GitHub remote)`.

Two hooks see edits -- orientation-check.sh (Edit, Write, NotebookEdit) and bash_actions.py (a file
changed by a command) -- and until 0.36.0 each kept its own list. The Bash one knew `/tmp`, the
Edit one knew no scratch space at all, and neither knew macOS, where `/tmp` and `/var` are links
into `/private` and every tool is handed the RESOLVED spelling (Claude Code's scratchpad is
`/private/tmp/claude-<uid>/...`, $TMPDIR is `/var/folders/...`). So the list lives here, once.

Ephemeral:
  - scratch space: /tmp and /var/tmp, their macOS spellings under /private, the per-user temp tree
    (/var/folders, /private/var/folders), and $TMPDIR when it names a real temp directory (not the
    filesystem root, not $HOME or a directory above it);
  - device and process files: /dev, /proc;
  - Claude Code's own state: <config>/projects (agent memory, transcripts) and <config>/plans,
    <config> being $CLAUDE_CONFIG_DIR or ~/.claude. An allowlist of two directories on purpose:
    <config>/plugins, agents, skills and commands are AUTHORED source and stay counted.

A checkout that happens to live in scratch space is still heard from: its commits, pull requests,
merges and releases are events, and events are never filtered by path.

Pure: the environment (HOME, CLAUDE_CONFIG_DIR, TMPDIR) is all it reads, and the match is lexical
(`..` is folded, symlinks are not looked at). Never raises.

CLI (for orientation-check.sh, through lib-state.sh's gks_is_ephemeral):
  python3 ephemeral.py PATH     print "yes" when PATH is ephemeral, nothing otherwise; exit 0

Tests: plugins/gitian-kb/hooks/tests/test_ephemeral.py.
"""

import os
import posixpath
import sys

SCRATCH_ROOTS = (
    "/tmp",
    "/var/tmp",
    "/private/tmp",
    "/private/var/tmp",
    "/var/folders",
    "/private/var/folders",
    "/dev",
    "/proc",
)


def under(path, root):
    return path == root or path.startswith(root + "/")


def _clean(path):
    """An absolute path with `.`, `..` and doubled slashes folded; None for anything else."""
    if not isinstance(path, str) or not path.startswith("/"):
        return None
    return posixpath.normpath("/" + path.lstrip("/"))


def _tmpdir(home):
    """$TMPDIR as a scratch root, unless calling it scratch could swallow real work: unset,
    relative, the filesystem root, or $HOME itself or a directory above it."""
    tmp = _clean(os.environ.get("TMPDIR"))
    if tmp is None or tmp == "/":
        return None
    if home is not None and under(home, tmp):
        return None
    return tmp


def roots():
    """Every ephemeral root for this environment, as normalized absolute paths."""
    found = list(SCRATCH_ROOTS)
    tmp = _tmpdir(_clean(os.environ.get("HOME")))
    if tmp is not None and tmp not in found:
        found.append(tmp)
    config = _clean(os.environ.get("CLAUDE_CONFIG_DIR") or (os.environ.get("HOME") or "") + "/.claude")
    if config is not None:
        found.extend((posixpath.join(config, "projects"), posixpath.join(config, "plans")))
    return found


def is_ephemeral(path):
    """Whether `path` (absolute) is somewhere a write is not the user's work. A relative or
    unreadable path is not: the caller keeps counting what it cannot place."""
    path = _clean(path)
    if path is None:
        return False
    return any(under(path, root) for root in roots())


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1 and is_ephemeral(sys.argv[1]):
            sys.stdout.write("yes\n")
    except Exception:
        pass  # fail-open: never a traceback, never a non-zero exit
    sys.exit(0)
