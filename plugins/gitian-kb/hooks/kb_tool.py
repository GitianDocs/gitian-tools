#!/usr/bin/env python3
"""kb_tool.py -- which MCP tool names belong to the gitian KNOWLEDGE BASE server.

Every nudge-layer hook is about the KB: its vocabulary cache, its publishes, its routing. A gitian
install now has a SECOND MCP server beside it -- the read-only code server (`code_repos`,
`code_overview`, `code_search`, `code_annotation`, `code_page`), wired by the gitian-docs plugin as
`gitian-code`. Its tool names also contain "gitian" and one of them ends in "search", so a hook
that asks "does the name contain gitian" or "does it end in a read suffix" would count a code
lookup as a KB orientation read. The question every hook asks is therefore keyed to the SERVER
SEGMENT of the tool name -- `mcp__<server>__<tool>` -- and only these two servers are the KB:

  mcp__plugin_gitian-kb_gitian-kb__<tool>   wired by the gitian-kb plugin (.mcp.json key `gitian-kb`)
  mcp__gitian-kb__<tool>                    wired by hand under the name `gitian-kb`

The server segment is compared case-insensitively (a hand-wired `Gitian-KB` is the same server); the
tool segment is returned exactly as given. hooks.json carries the same two spellings as a matcher
(`mcp__(plugin_gitian-kb_)?[gG][iI][tT][iI][aA][nN]-[kK][bB]__`) -- spelled with character
classes because a regex matcher has no flag for case -- and this module is the precise check
behind that pre-filter. The server key was `gitian` before 0.28.0; that name is no longer the KB.

Stdlib only; imported as a sibling module by the hook scripts that run from this directory.
"""

# Lowercased: callers compare against name.lower(). The trailing "__" is what separates the server
# segment from the tool, so `mcp__gitian-kb-x__get` can never start with `mcp__gitian-kb__`, and
# neither can the code server (`gitian-code`) or a server hand-wired under the old name `gitian`.
KB_SERVER_PREFIXES = ("mcp__plugin_gitian-kb_gitian-kb__", "mcp__gitian-kb__")


def kb_tool_name(tool_name):
    """The bare tool name (`publish_doc`, `search`, ...) when `tool_name` is a tool of the KB
    server under either spelling, else None -- including a non-string, the code server's tools and
    any other MCP server's."""
    if not isinstance(tool_name, str):
        return None
    lowered = tool_name.lower()
    for prefix in KB_SERVER_PREFIXES:
        if lowered.startswith(prefix):
            bare = tool_name[len(prefix):]
            return bare if bare else None
    return None


def is_kb_tool(tool_name):
    return kb_tool_name(tool_name) is not None
