"""Unit tests for the MCP server wiring (SPEC §5.2).

Seam under test: ``build_server`` (``src/hivemind/mcp/server.py``). The
stdio transport itself is not exercised (it would block on stdin), so
the seam is: ``build_server`` registers exactly the six tool names, and a
registered tool, invoked through ``call_tool``, delegates to the plain
functions in ``app.py`` and returns a well-formed result.
"""

from __future__ import annotations

import json

import pytest

from hivemind.mcp.app import McpHivemind, hive_write
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import make_clock, make_embedder, make_search_config

EXPECTED_TOOLS = [
    "hive_write",
    "hive_search",
    "hive_get",
    "hive_list",
    "hive_withdraw",
    "hive_feedback",
]


def make_app() -> McpHivemind:
    """A minimal MemoryStore-backed ``McpHivemind`` for the server tests."""
    clock = make_clock()
    store = MemoryStore(clock)
    embedder = make_embedder()
    config = make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        search_config=config,
        credential=Credential(user_id="dev", agent_id="hivemind-mcp"),
    )


@pytest.fixture
def app() -> McpHivemind:
    return make_app()


async def test_build_server_registers_exactly_six_tools(app: McpHivemind) -> None:
    server = build_server(app)
    tools = await server.list_tools()
    names = sorted(t.name for t in tools)
    assert names == sorted(EXPECTED_TOOLS)


async def test_registered_write_tool_roundtrip(app: McpHivemind) -> None:
    """The registered ``hive_write`` tool, called through ``call_tool``,
    delegates to the plain function and persists into the store."""
    server = build_server(app)
    result = await server.call_tool("hive_write", {"kind": "fact", "summary": "hello via mcp"})
    assert result.is_error is False
    assert len(result.content) == 1
    payload = json.loads(result.content[0].text)
    assert payload["kind"] == "fact"
    assert "id" in payload
    # The write went through the plain function into the shared store.
    stored = await app.store.get_entry(payload["id"])
    assert stored is not None
    assert stored.summary == "hello via mcp"


async def test_registered_get_tool_returns_error_for_unknown_id(
    app: McpHivemind,
) -> None:
    server = build_server(app)
    written = await hive_write(app, kind="fact", summary="known entry")
    result = await server.call_tool("hive_get", {"entry_id": written["id"]})
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["id"] == written["id"]

    missing = await server.call_tool("hive_get", {"entry_id": "no-such-id"})
    assert json.loads(missing.content[0].text)["error"]["code"] == "not_found"
