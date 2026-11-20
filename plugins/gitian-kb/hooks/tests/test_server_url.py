#!/usr/bin/env python3
"""Unit tests for server_url.py, the one place a gitian-kb hook learns which KB server it talks to.

The plugin's `.mcp.json` connects `${GITIAN_KB_URL:-https://gitian.dev}/api/mcp/kb`, and every hook
files what it caches about that server under `server_key()`. For every value of GITIAN_KB_URL the
MCP client can sensibly be given, the key is byte for byte the URL the shell expands that template
to (asserted against a real `sh` here, not a re-implementation of it). A trailing slash is the one
place the two part: the key drops it. That is safe because the key is written and read by the hooks
alone -- a hook's payload names only the tool, never the URL the client connected to -- so nothing
compares the two strings.

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_server_url.py
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOKS_DIR))

import server_url  # noqa: E402  (the module under test lives one directory up)

MCP_JSON = HOOKS_DIR.parent / ".mcp.json"
# The four hooks that read or write a server's cached state.
SERVER_STATE_HOOKS = ("harvest.py", "publish_lint.py", "routing_guard.py", "session_digest.py")


def _env_without_override():
    env = dict(os.environ)
    env.pop("GITIAN_KB_URL", None)
    return env


def _shell_expansion(env):
    """What the MCP client is handed: `.mcp.json`'s url, expanded by a real POSIX shell."""
    template = json.loads(MCP_JSON.read_text(encoding="utf-8"))["gitian-kb"]["url"]
    proc = subprocess.run(
        ["sh", "-c", 'printf "%s" "' + template + '"'],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


class ServerUrlTestCase(unittest.TestCase):
    def under(self, value):
        """Patch GITIAN_KB_URL to `value` (None = unset) for the duration of one block."""
        env = _env_without_override()
        if value is not None:
            env["GITIAN_KB_URL"] = value
        return mock.patch.dict(os.environ, env, clear=True)


class Defaults(ServerUrlTestCase):
    def test_unset_is_the_hosted_server(self):
        with self.under(None):
            self.assertEqual(server_url.server_url(), "https://gitian.dev")
            self.assertEqual(server_url.server_key(), "https://gitian.dev/api/mcp/kb")

    def test_empty_is_the_same_as_unset(self):
        with self.under(""):
            self.assertEqual(server_url.server_url(), "https://gitian.dev")
            self.assertEqual(server_url.server_key(), "https://gitian.dev/api/mcp/kb")


class ConfiguredServer(ServerUrlTestCase):
    def test_a_value_with_no_trailing_slash_is_kept_exactly(self):
        for value in (
            "https://kb.acme.test",
            "http://localhost:8080",
            "https://kb.acme.test/team",
            "HTTPS://KB.Acme.Test:8443",
        ):
            with self.subTest(value=value), self.under(value):
                self.assertEqual(server_url.server_url(), value)
                self.assertEqual(server_url.server_key(), value + "/api/mcp/kb")

    def test_a_trailing_slash_names_the_same_server(self):
        for value in ("https://kb.acme.test/", "https://kb.acme.test//"):
            with self.subTest(value=value), self.under(value):
                self.assertEqual(server_url.server_url(), "https://kb.acme.test")
                self.assertEqual(server_url.server_key(), "https://kb.acme.test/api/mcp/kb")

    def test_a_value_of_only_slashes_is_the_same_as_unset(self):
        with self.under("/"):
            self.assertEqual(server_url.server_key(), "https://gitian.dev/api/mcp/kb")

    def test_it_is_read_on_every_call_never_cached_at_import(self):
        with self.under("https://kb.acme.test"):
            self.assertEqual(server_url.server_key(), "https://kb.acme.test/api/mcp/kb")
        with self.under(None):
            self.assertEqual(server_url.server_key(), "https://gitian.dev/api/mcp/kb")


class MirrorsTheClientsUrl(ServerUrlTestCase):
    """Byte for byte what `.mcp.json` expands to, wherever the client's URL is well formed."""

    def test_the_key_is_the_shell_expansion_for_unset_empty_and_slashless_values(self):
        for value in (None, "", "https://kb.acme.test", "http://127.0.0.1:3000"):
            with self.subTest(value=value), self.under(value):
                env = _env_without_override()
                if value is not None:
                    env["GITIAN_KB_URL"] = value
                self.assertEqual(server_url.server_key(), _shell_expansion(env))

    def test_with_a_trailing_slash_the_key_drops_what_the_shell_keeps(self):
        # The client is handed `https://kb.acme.test//api/mcp/kb`; the hooks file its state under
        # the single-slash key. Nothing a hook reads carries the client's URL, so the two never meet.
        env = _env_without_override()
        env["GITIAN_KB_URL"] = "https://kb.acme.test/"
        self.assertEqual(_shell_expansion(env), "https://kb.acme.test//api/mcp/kb")
        with self.under("https://kb.acme.test/"):
            self.assertEqual(server_url.server_key(), "https://kb.acme.test/api/mcp/kb")


class TheOnePlace(unittest.TestCase):
    def test_every_hook_that_keeps_server_state_imports_it(self):
        for name in SERVER_STATE_HOOKS:
            with self.subTest(hook=name):
                source = (HOOKS_DIR / name).read_text(encoding="utf-8")
                self.assertIn("import server_url", source)
                self.assertIn("server_url.server_key()", source)

    def test_no_other_hook_reads_the_variable_or_keeps_a_copy_of_the_key(self):
        for path in sorted(HOOKS_DIR.iterdir()):
            if path.suffix not in (".py", ".sh") or path.name == "server_url.py":
                continue
            with self.subTest(hook=path.name):
                source = path.read_text(encoding="utf-8")
                self.assertNotIn("GITIAN_KB_URL", source)
                self.assertNotIn("def _server_key", source)
                self.assertNotIn('"/api/mcp/kb"', source)


if __name__ == "__main__":
    unittest.main()
