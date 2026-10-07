#!/usr/bin/env python3
"""Unit tests for kb_tool.py, the one place the gitian-kb hooks decide whether a tool name belongs
to the gitian KNOWLEDGE BASE server (as opposed to the read-only code server, or anything else).

Runnable directly: python3 plugins/gitian-kb/hooks/tests/test_kb_tool.py
"""

import sys
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOKS_DIR))

import kb_tool  # noqa: E402  (the module under test lives one directory up)


class KbToolName(unittest.TestCase):
    def test_both_kb_spellings_resolve_to_the_bare_tool_name(self):
        self.assertEqual(kb_tool.kb_tool_name("mcp__plugin_gitian-kb_gitian__publish_doc"), "publish_doc")
        self.assertEqual(kb_tool.kb_tool_name("mcp__gitian__read_resource"), "read_resource")

    def test_the_server_segment_is_case_insensitive_and_the_tool_segment_is_kept_as_given(self):
        self.assertEqual(kb_tool.kb_tool_name("mcp__Gitian__search"), "search")
        self.assertEqual(kb_tool.kb_tool_name("mcp__GITIAN__Search"), "Search")
        self.assertEqual(kb_tool.kb_tool_name("MCP__plugin_Gitian-KB_gitian__get"), "get")

    def test_the_code_servers_tools_are_not_kb_tools_under_either_spelling(self):
        for name in (
            "mcp__plugin_gitian-docs_gitian-code__code_search",
            "mcp__plugin_gitian-docs_gitian-code__code_page",
            "mcp__gitian-code__code_repos",
            "mcp__gitian-code__code_overview",
            "mcp__gitian-code__code_annotation",
            # a code-server tool that borrowed a KB tool's bare name is still the code server's
            "mcp__gitian-code__search",
        ):
            with self.subTest(name=name):
                self.assertIsNone(kb_tool.kb_tool_name(name))
                self.assertFalse(kb_tool.is_kb_tool(name))

    def test_other_servers_and_non_strings_are_not_kb_tools(self):
        for name in (
            "mcp__other__publish_doc",
            "mcp__gitian-kb__get",
            "mcp__gitian__",
            "mcp__gitian",
            "gitian_publish_doc",
            "Bash",
            "",
            None,
            42,
            ["mcp__gitian__get"],
        ):
            with self.subTest(name=name):
                self.assertFalse(kb_tool.is_kb_tool(name))


if __name__ == "__main__":
    unittest.main()
