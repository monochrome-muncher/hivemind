"""Search hits carry ``scope`` and ``fleet_id`` (ADR 0036), so a privileged
reader can recognise a foreign entry — one filed outside its home fleet —
without opening it, on both surfaces."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.mcp.app import McpHivemind, hive_search, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config


def _agent(name: str, fleet: str, level: TrustLevel) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=level,
        home_fleet_id=fleet,
    )


WRITER = _agent("writer", "fleet-b", TrustLevel.CONTRIBUTOR)
READER = _agent("reader", "fleet-a", TrustLevel.PRIVILEGED)


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


async def test_mcp_hits_show_scope_and_fleet() -> None:
    store = MemoryStore(make_clock())
    await hive_write(_surface(store, WRITER), kind="fact", summary="billing runs on node 20")
    result = await hive_search(_surface(store, READER), query="billing node")
    [hit] = result["hits"]  # type: ignore[index]
    assert (hit["scope"], hit["fleet_id"]) == ("fleet", "fleet-b")
    assert hit["fleet_id"] != READER.home_fleet_id  # a foreign entry


async def test_rest_hits_show_scope_and_fleet() -> None:
    store = MemoryStore(make_clock())
    await hive_write(_surface(store, WRITER), kind="fact", summary="billing runs on node 20")
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": READER}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/v1/search", json={"query": "billing node"}, headers={"X-API-Key": "k"}
        )
    [hit] = resp.json()
    assert (hit["scope"], hit["fleet_id"]) == ("fleet", "fleet-b")
