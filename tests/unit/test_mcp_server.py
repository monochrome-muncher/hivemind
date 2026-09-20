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

from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntityKind, EntryDraft, ExtractedEntity, Kind
from hivemind.mcp.app import McpHivemind, hive_write
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
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
    "hive_register",
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
        access_service=AccessService(store),
        search_config=config,
        credential=Credential(user_id="dev", agent_id="hivemind-mcp"),
    )


@pytest.fixture
def app() -> McpHivemind:
    return make_app()


async def test_build_server_registers_expected_tools(app: McpHivemind) -> None:
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


# --- registered-wrapper defaults (ROADMAP 1.2 dogfood findings) -------------
# The registered tool wrapper (``server.py``) generates the MCP input
# schema; a forced default there bypasses the app-layer fix. These tests
# call the *registered* tool the way a real MCP client would.


async def _l2_app() -> tuple[McpHivemind, str]:
    """An L2 (contributor) agent with a home fleet, wired into a server."""
    clock = make_clock()
    store = MemoryStore(clock)
    embedder = make_embedder()
    config = make_search_config()
    fleet = await store.create_fleet("eng")
    await store.register_agent("carol")
    await store.activate_agent("carol", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id)
    cred = Credential(
        user_id="carol",
        agent_id="carol",
        agent_name="carol",
        access_controlled=True,
        trust_level=TrustLevel.CONTRIBUTOR,
        home_fleet_id=fleet.id,
    )
    app = McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        access_service=AccessService(store),
        search_config=config,
        credential=cred,
    )
    return app, fleet.id


async def test_registered_write_omitted_scope_defaults_to_max_permitted() -> None:
    """The *registered* hive_write wrapper must not force scope='org'
    (ADR 0011: an omitted scope defaults to the highest scope the trust
    level permits — a forced 'org' default rejected every L2/L1 write;
    found in the ROADMAP 1.2 dogfood)."""
    app, fleet_id = await _l2_app()
    server = build_server(app)
    # Omit 'scope' entirely: the wrapper must not inject its own default.
    result = await server.call_tool(
        "hive_write", {"kind": "fact", "summary": "omitted scope via mcp"}
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["scope"] == "fleet"
    assert payload["fleet_id"] == fleet_id


async def test_registered_get_empty_entry_id_is_invalid_input() -> None:
    """An empty entry_id is a caller error, not 'unknown entry: ' (dogfood)."""
    app, _ = await _l2_app()
    server = build_server(app)
    result = await server.call_tool("hive_get", {"entry_id": ""})
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == "invalid_input"
    assert "entry_id" in payload["error"]["message"]


async def test_registered_feedback_empty_entry_id_is_invalid_input() -> None:
    """Same guard on the registered hive_feedback tool."""
    app, _ = await _l2_app()
    server = build_server(app)
    result = await server.call_tool("hive_feedback", {"entry_id": "", "verdict": "helpful"})
    payload = json.loads(result.content[0].text)
    assert payload["error"]["code"] == "invalid_input"
    assert "entry_id" in payload["error"]["message"]


# --- entity facets (ADR 0016, SPEC §13): registered tool wrappers ----------- #


async def _app_with_entity_entry() -> McpHivemind:
    """A minimal app holding one entry that carries extracted facets."""
    app = make_app()
    await app.store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="Postgres connection pool exhausted",
            author="dev",
            agent="hivemind-mcp",
        ),
        entities=(ExtractedEntity(name="Postgres", kind=EntityKind.SYSTEM),),
        entities_model="test-extractor",
    )
    return app


async def test_registered_search_tool_accepts_entities_param() -> None:
    """The registered hive_search wrapper accepts the machine-extracted
    entity-name filter (ADR 0016) and delegates it to the app layer."""
    app = await _app_with_entity_entry()
    server = build_server(app)
    hit = await server.call_tool(
        "hive_search", {"query": "postgres pool", "entities": ["postgres"]}
    )
    assert hit.is_error is False
    payload = json.loads(hit.content[0].text)
    assert payload["count"] == 1
    # AND-semantics: no entry carries both names.
    none = await server.call_tool(
        "hive_search", {"query": "pool", "entities": ["postgres", "kubernetes"]}
    )
    assert json.loads(none.content[0].text)["count"] == 0


async def test_registered_list_tool_accepts_entities_param() -> None:
    """The registered hive_list wrapper accepts the entity-name filter."""
    app = await _app_with_entity_entry()
    server = build_server(app)
    result = await server.call_tool("hive_list", {"entities": ["postgres"]})
    assert json.loads(result.content[0].text)["count"] == 1
    none = await server.call_tool("hive_list", {"entities": ["postgres", "kubernetes"]})
    assert json.loads(none.content[0].text)["count"] == 0


async def test_registered_tool_schema_exposes_entities_param() -> None:
    """The generated MCP input schema for hive_search / hive_list includes
    the optional 'entities' param (mirroring 'tags')."""
    server = build_server(make_app())
    tools = {t.name: t for t in await server.list_tools()}
    for name in ("hive_search", "hive_list"):
        props = tools[name].input_schema.get("properties", {})
        assert "entities" in props, f"{name} schema is missing the 'entities' param"
