"""One verdict on several entries at once (ADR 0053): all readable or
nothing recorded, on both surfaces."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.validation import MAX_FEEDBACK_IDS
from hivemind.mcp.app import McpHivemind, hive_feedback, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, WriteService
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


WRITER = _agent("writer", "fleet-a")
READER = _agent("reader", "fleet-a")
OUTSIDER = _agent("outsider", "fleet-b")


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


async def _entries(store: MemoryStore, credential: Credential, n: int) -> list[str]:
    app = _surface(store, credential)
    return [str((await hive_write(app, kind="fact", summary=f"fact {i}"))["id"]) for i in range(n)]


async def test_mcp_one_verdict_lands_on_every_entry() -> None:
    store = MemoryStore(make_clock())
    ids = await _entries(store, WRITER, 3)
    result = await hive_feedback(
        _surface(store, READER), verdict="helpful", note="fixed the deploy", entry_ids=ids
    )
    assert "error" not in result
    assert [r["entry_id"] for r in result["results"]] == ids  # type: ignore[index]
    for eid in ids:
        assert await store.feedback_counts(eid) == (1, 0, 0)
        [row] = await store.list_feedback(eid, 5)
        assert (row.user, row.note) == ("reader", "fixed the deploy")


async def test_repeated_ids_count_once() -> None:
    store = MemoryStore(make_clock())
    [eid] = await _entries(store, WRITER, 1)
    result = await hive_feedback(_surface(store, READER), verdict="stale", entry_ids=[eid, eid])
    assert len(result["results"]) == 1  # type: ignore[arg-type]
    assert await store.feedback_counts(eid) == (0, 1, 0)


async def test_an_unreadable_id_records_nothing() -> None:
    store = MemoryStore(make_clock())
    ours = await _entries(store, WRITER, 2)
    [theirs] = await _entries(store, OUTSIDER, 1)
    result = await hive_feedback(
        _surface(store, READER), verdict="wrong", entry_ids=[*ours, theirs, "no-such-id"]
    )
    error = result["error"]
    assert error["code"] == "not_found"  # type: ignore[index]
    assert theirs in error["message"] and "no-such-id" in error["message"]  # type: ignore[index]
    assert ours[0] not in error["message"]  # type: ignore[index]
    for eid in ours:
        assert await store.feedback_counts(eid) == (0, 0, 0)


async def test_too_many_or_no_ids_is_invalid_input() -> None:
    store = MemoryStore(make_clock())
    app = _surface(store, READER)
    too_many = [f"id-{i}" for i in range(MAX_FEEDBACK_IDS + 1)]
    for ids in (too_many, []):
        result = await hive_feedback(app, verdict="helpful", entry_ids=ids)
        assert result["error"]["code"] == "invalid_input"  # type: ignore[index]


async def test_entry_id_and_entry_ids_together_is_invalid_input() -> None:
    store = MemoryStore(make_clock())
    [eid] = await _entries(store, WRITER, 1)
    result = await hive_feedback(
        _surface(store, READER), entry_id=eid, verdict="helpful", entry_ids=[eid]
    )
    assert result["error"]["code"] == "invalid_input"  # type: ignore[index]


async def test_a_bad_verdict_is_rejected_before_anything_is_recorded() -> None:
    store = MemoryStore(make_clock())
    [eid] = await _entries(store, WRITER, 1)
    result = await hive_feedback(_surface(store, READER), verdict="meh", entry_ids=[eid])
    assert result["error"]["code"] == "invalid_verdict"  # type: ignore[index]
    assert await store.feedback_counts(eid) == (0, 0, 0)


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


async def test_rest_batch_feedback() -> None:
    store = MemoryStore(make_clock())
    ids = await _entries(store, WRITER, 2)
    async with _client(store, READER) as client:
        resp = await client.post(
            "/v1/feedback", json={"entry_ids": ids, "verdict": "helpful", "note": "n"}
        )
    assert resp.status_code == 200
    assert [r["entry_id"] for r in resp.json()] == ids
    assert all(r["verdict"] == "helpful" for r in resp.json())


async def test_rest_batch_feedback_unreadable_is_404_and_records_nothing() -> None:
    store = MemoryStore(make_clock())
    [ours] = await _entries(store, WRITER, 1)
    [theirs] = await _entries(store, OUTSIDER, 1)
    async with _client(store, READER) as client:
        resp = await client.post(
            "/v1/feedback", json={"entry_ids": [ours, theirs], "verdict": "wrong"}
        )
    assert resp.status_code == 404
    assert await store.feedback_counts(ours) == (0, 0, 0)


async def test_rest_batch_feedback_bounds() -> None:
    store = MemoryStore(make_clock())
    async with _client(store, READER) as client:
        empty = await client.post("/v1/feedback", json={"entry_ids": [], "verdict": "helpful"})
        many = await client.post(
            "/v1/feedback",
            json={"entry_ids": [f"i{n}" for n in range(MAX_FEEDBACK_IDS + 1)], "verdict": "stale"},
        )
    assert empty.status_code == 422
    assert many.status_code == 422
