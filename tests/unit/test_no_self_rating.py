"""An author cannot rate its own entry (ADR 0065): refused on both
surfaces, single and batch, with nothing recorded; readability first."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.mcp.app import McpHivemind, hive_feedback, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config


def _agent(name: str, fleet: str) -> Credential:
    return Credential(
        user_id=name,
        agent_id=name,
        agent_name=name,
        access_controlled=True,
        trust_level=TrustLevel.CONTRIBUTOR,
        home_fleet_id=fleet,
    )


ME = _agent("me", "fleet-a")
PEER = _agent("peer", "fleet-a")
OUTSIDER = _agent("outsider", "fleet-b")


def _surface(store: MemoryStore, credential: Credential) -> McpHivemind:
    clock, embedder, config = make_clock(), make_embedder(), make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        read_service=ReadService(store, GovernanceService(store), config),
        access_service=AccessService(store),
        search_config=config,
        credential=credential,
    )


async def _write(store: MemoryStore, credential: Credential, summary: str) -> str:
    return str((await hive_write(_surface(store, credential), kind="fact", summary=summary))["id"])


def _client(store: MemoryStore, credential: Credential) -> httpx.AsyncClient:
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": credential}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    return httpx.AsyncClient(transport=transport, base_url="http://t", headers={"X-API-Key": "k"})


async def test_mcp_single_own_entry_is_refused() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    result = await hive_feedback(_surface(store, ME), mine, verdict="helpful")
    assert result["error"]["code"] == "permission_denied"  # type: ignore[index]
    assert "ADR 0065" in result["error"]["message"]  # type: ignore[index]
    assert await store.feedback_counts(mine) == (0, 0, 0)


async def test_mcp_someone_elses_entry_still_works() -> None:
    store = MemoryStore(make_clock())
    theirs = await _write(store, PEER, "theirs")
    result = await hive_feedback(_surface(store, ME), theirs, verdict="helpful")
    assert "error" not in result
    assert await store.feedback_counts(theirs) == (1, 0, 0)


async def test_mcp_batch_with_an_own_entry_records_nothing() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    theirs = await _write(store, PEER, "theirs")
    result = await hive_feedback(_surface(store, ME), verdict="wrong", entry_ids=[theirs, mine])
    assert result["error"]["code"] == "permission_denied"  # type: ignore[index]
    assert mine in result["error"]["message"]  # type: ignore[index]
    assert theirs not in result["error"]["message"]  # type: ignore[index]
    assert await store.feedback_counts(theirs) == (0, 0, 0)
    assert await store.feedback_counts(mine) == (0, 0, 0)


async def test_batch_of_only_others_entries_works() -> None:
    store = MemoryStore(make_clock())
    ids = [await _write(store, PEER, f"fact {i}") for i in range(2)]
    result = await hive_feedback(_surface(store, ME), verdict="helpful", entry_ids=ids)
    assert "error" not in result


async def test_an_unreadable_id_is_still_not_found_beside_an_own_one() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    theirs = await _write(store, OUTSIDER, "elsewhere")
    result = await hive_feedback(_surface(store, ME), verdict="helpful", entry_ids=[mine, theirs])
    assert result["error"]["code"] == "not_found"  # type: ignore[index]
    single = await hive_feedback(_surface(store, ME), theirs, verdict="helpful")
    assert single["error"]["code"] == "not_found"  # type: ignore[index]


async def test_rest_single_own_entry_is_403() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    async with _client(store, ME) as client:
        resp = await client.post(f"/v1/entries/{mine}/feedback", json={"verdict": "helpful"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert "ADR 0065" in resp.json()["error"]["message"]
    assert await store.feedback_counts(mine) == (0, 0, 0)


async def test_rest_someone_elses_entry_still_works() -> None:
    store = MemoryStore(make_clock())
    theirs = await _write(store, PEER, "theirs")
    async with _client(store, ME) as client:
        resp = await client.post(f"/v1/entries/{theirs}/feedback", json={"verdict": "helpful"})
    assert resp.status_code == 200


async def test_rest_batch_with_an_own_entry_is_403_and_records_nothing() -> None:
    store = MemoryStore(make_clock())
    mine = await _write(store, ME, "mine")
    theirs = await _write(store, PEER, "theirs")
    async with _client(store, ME) as client:
        resp = await client.post(
            "/v1/feedback", json={"entry_ids": [theirs, mine], "verdict": "wrong"}
        )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    assert mine in resp.json()["error"]["message"]
    assert await store.feedback_counts(theirs) == (0, 0, 0)
    assert await store.feedback_counts(mine) == (0, 0, 0)
