"""The Hivemind MCP server package (SPEC §5.2).

Exposes the six plain tool functions (``hive_write``, ``hive_search``,
``hive_get``, ``hive_list``, ``hive_withdraw``, ``hive_feedback``), the
``McpHivemind`` app object, and ``build_server`` / ``main`` for the
stdio transport.
"""

from __future__ import annotations

from hivemind.mcp.app import (
    McpHivemind,
    hive_feedback,
    hive_get,
    hive_list,
    hive_search,
    hive_withdraw,
    hive_write,
)
from hivemind.mcp.server import build_server, main

__all__ = [
    "McpHivemind",
    "build_server",
    "hive_feedback",
    "hive_get",
    "hive_list",
    "hive_search",
    "hive_withdraw",
    "hive_write",
    "main",
]