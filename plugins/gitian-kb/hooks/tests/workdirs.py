#!/usr/bin/env python3
"""workdirs.py -- a directory the hooks count as WORK, for a test that needs a checkout to edit.

Everything under scratch space is not the user's work (hooks/ephemeral.py: /tmp, $TMPDIR, the macOS
spellings), by design, and that is exactly where tempfile.mkdtemp() makes its directories. So a
checkout whose edits a test expects to see counted is made inside the real home directory instead,
dot-prefixed, and removed by the caller. State files, fake binaries and logs stay in a plain
tempdir: nothing counts those.

Not a test file (the suite sweeps `test_*.py`); every test file that needs it imports it from this
directory, which is on sys.path both when a file is run directly and under `unittest discover`.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import ephemeral  # noqa: E402  (the hooks' own list decides what is scratch)


def make_work_dir(prefix):
    """A fresh directory outside every ephemeral root. Skips the calling test when there is nowhere
    to make one: a home directory that cannot be written, or that is itself scratch space."""
    try:
        path = tempfile.mkdtemp(prefix="." + prefix, dir=os.path.expanduser("~"))
    except OSError:
        raise unittest.SkipTest("the home directory is not writable and everywhere else is scratch")
    if ephemeral.is_ephemeral(path):
        shutil.rmtree(path, True)
        raise unittest.SkipTest("the home directory is itself scratch space")
    return path
