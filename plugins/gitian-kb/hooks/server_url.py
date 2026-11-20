#!/usr/bin/env python3
"""server_url.py -- the one place a gitian-kb hook learns which KB server it is talking to.

The plugin's `.mcp.json` connects the MCP client to `${GITIAN_KB_URL:-https://gitian.dev}/api/mcp/kb`.
A hook never sees that URL: a tool call's payload names only the tool
(`mcp__plugin_gitian-kb_gitian-kb__get`), so the hooks derive the server from the same variable and
file everything they cache about it -- vocabulary revision and topics, the connection's default KB,
the advertised plugin version -- under `servers.<server_key()>` in the state file. That key is
written and read by the hooks alone; nothing compares it with the URL the client connected to.

  server_url()  the configured server: GITIAN_KB_URL, or https://gitian.dev when it is unset or
                empty (the shell's `:-`). Trailing slashes are dropped, so `https://kb.example/`
                and `https://kb.example` are one server with one cache.
  server_key()  server_url() + "/api/mcp/kb": for every value without a trailing slash, byte for
                byte the URL `.mcp.json` expands to.

Both read the environment on every call. Stdlib only; imported as a sibling module by the hook
scripts that run from this directory (tests: tests/test_server_url.py).
"""

import os

DEFAULT_SERVER_URL = "https://gitian.dev"
KB_ENDPOINT_PATH = "/api/mcp/kb"


def server_url():
    """GITIAN_KB_URL without trailing slashes, or the default when that leaves nothing."""
    return (os.environ.get("GITIAN_KB_URL") or "").rstrip("/") or DEFAULT_SERVER_URL


def server_key():
    """The key every hook files this server's state under."""
    return server_url() + KB_ENDPOINT_PATH
