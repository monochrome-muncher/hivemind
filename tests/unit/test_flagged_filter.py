"""The ``flagged`` filter (ADR 0054): only entries reported stale or wrong,
on list and search, both surfaces."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryFilters
from hivemind.mcp.app import McpHivemind, hive_feedback, hive_list, hive_search, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config


def _agent(name: str, level: TrustLevel) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id="fleet-a",
    )


WRITER = _agent("writer", TrustLevel.CONTRIBUTOR)
LURKER = _agent("lurker", TrustLevel.LURKER)


def _surface(store: MemoryStore, credential: Credential) -> McpHivemind:
    clock, embedder, config = make_clock(), make_embedder(), make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        access_service=AccessService(store),
        search_config=config,
        credential=credential,
    )


async def _seed(store: MemoryStore) -> dict[str, str]:
    """Four billing entries: one stale, one wrong, one helpful, one untouched."""
    writer, lurker = _surface(store, WRITER), _surface(store, LURKER)
    ids: dict[str, str] = {}
    for name in ("stale", "wrong", "helpful", "untouched"):
        entry = await hive_write(writer, kind="fact", summary=f"billing {name} fact")
        ids[name] = str(entry["id"])
    for verdict in ("stale", "wrong", "helpful"):
        await hive_feedback(lurker, ids[verdict], verdict=verdict, note="checked")
    return ids


async def test_mcp_list_flagged_returns_stale_and_wrong_only() -> None:
    store = MemoryStore(make_clock())
    ids = await _seed(store)
    result = await hive_list(_surface(store, WRITER), flagged=True)
    assert {e["id"] for e in result["entries"]} == {ids["stale"], ids["wrong"]}  # type: ignore[index]
    unfiltered = await hive_list(_surface(store, WRITER))
    assert len(unfiltered["entries"]) == 4  # type: ignore[arg-type]


async def test_mcp_search_flagged() -> None:
    store = MemoryStore(make_clock())
    ids = await _seed(store)
    result = await hive_search(_surface(store, WRITER), query="billing fact", flagged=True)
    assert {h["id"] for h in result["hits"]} == {ids["stale"], ids["wrong"]}  # type: ignore[index]


async def test_flagged_hides_entries_already_superseded() -> None:
    """A flagged entry someone superseded is fixed: it leaves the queue."""
    store = MemoryStore(make_clock())
    ids = await _seed(store)
    await hive_write(
        _surface(store, WRITER),
        kind="fact",
        summary="billing fact, corrected",
        supersedes=[ids["stale"]],
    )
    result = await hive_list(_surface(store, WRITER), flagged=True)
    assert {e["id"] for e in result["entries"]} == {ids["wrong"]}  # type: ignore[index]


async def test_flagged_counts_through_the_store() -> None:
    store = MemoryStore(make_clock())
    await _seed(store)
    assert await store.count_entries(EntryFilters(flagged=True)) == 2


async def test_rest_list_and_search_flagged() -> None:
    store = MemoryStore(make_clock())
    ids = await _seed(store)
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": WRITER}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"X-API-Key": "k"}
    ) as client:
        listed = await client.get("/v1/entries", params={"flagged": "true"})
        searched = await client.post("/v1/search", json={"query": "billing fact", "flagged": True})
    assert {e["id"] for e in listed.json()} == {ids["stale"], ids["wrong"]}
    assert {h["entry_id"] for h in searched.json()} == {ids["stale"], ids["wrong"]}
