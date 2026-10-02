"""Reading several entries in one call (ADR 0055), on both surfaces."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import Entry
from hivemind.domain.feedback import Feedback, FeedbackCounts
from hivemind.domain.validation import MAX_GET_IDS
from hivemind.mcp.app import McpHivemind, hive_feedback, hive_get, hive_write
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


async def _write(store: MemoryStore, credential: Credential, summary: str) -> str:
    entry = await hive_write(_surface(store, credential), kind="fact", summary=summary, body="b")
    return str(entry["id"])


async def test_mcp_reads_several_entries_in_the_order_asked() -> None:
    store = MemoryStore(make_clock())
    a = await _write(store, READER, "first fact")
    b = await _write(store, READER, "second fact")
    await hive_feedback(_surface(store, READER), b, verdict="stale", note="moved")
    result = await hive_get(_surface(store, READER), entry_ids=[b, a, b])
    entries = result["entries"]
    assert [e["id"] for e in entries] == [b, a]  # type: ignore[index]
    assert entries[0]["body"] == "b"  # type: ignore[index]
    assert entries[0]["feedback"]["stale"] == 1  # type: ignore[index]
    assert entries[0]["feedback"]["recent"][0]["note"] == "moved"  # type: ignore[index]
    assert result["not_found"] == []


async def test_unreadable_and_unknown_ids_are_not_found_not_errors() -> None:
    store = MemoryStore(make_clock())
    ours = await _write(store, READER, "our fact")
    theirs = await _write(store, OUTSIDER, "their fact")
    result = await hive_get(_surface(store, READER), entry_ids=[theirs, ours, "nope", "x\x00"])
    assert [e["id"] for e in result["entries"]] == [ours]  # type: ignore[index]
    assert result["not_found"] == [theirs, "nope", "x\x00"]


async def test_bounds_and_conflicting_arguments_are_invalid_input() -> None:
    store = MemoryStore(make_clock())
    app = _surface(store, READER)
    eid = await _write(store, READER, "a fact")
    for kwargs in (
        {"entry_ids": []},
        {"entry_ids": [f"id-{i}" for i in range(MAX_GET_IDS + 1)]},
        {"entry_ids": [eid], "entry_id": eid},
        {"entry_ids": [eid], "include_history": True},
    ):
        result = await hive_get(app, **kwargs)  # type: ignore[arg-type]
        assert result["error"]["code"] == "invalid_input", kwargs  # type: ignore[index]


async def test_single_entry_get_is_unchanged() -> None:
    store = MemoryStore(make_clock())
    eid = await _write(store, READER, "a fact")
    result = await hive_get(_surface(store, READER), eid)
    assert result["id"] == eid


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


async def test_rest_reads_several_entries() -> None:
    store = MemoryStore(make_clock())
    a = await _write(store, READER, "first fact")
    theirs = await _write(store, OUTSIDER, "their fact")
    async with _client(store, READER) as client:
        resp = await client.post("/v1/entries/get", json={"entry_ids": [a, theirs]})
        single = await client.get(f"/v1/entries/{a}")
    assert resp.status_code == 200
    body = resp.json()
    assert [e["id"] for e in body["entries"]] == [a]
    assert body["entries"][0]["feedback"] == single.json()["feedback"]
    assert body["not_found"] == [theirs]


async def test_rest_bounds() -> None:
    store = MemoryStore(make_clock())
    async with _client(store, READER) as client:
        empty = await client.post("/v1/entries/get", json={"entry_ids": []})
        many = await client.post(
            "/v1/entries/get", json={"entry_ids": [f"i{n}" for n in range(MAX_GET_IDS + 1)]}
        )
    assert (empty.status_code, many.status_code) == (422, 422)


class _CountingStore(MemoryStore):
    """Counts the store reads a batch read makes (ADR 0059)."""

    calls = 0

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        self.calls += 1
        return await super().get_entries(entry_ids)

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, FeedbackCounts]:
        self.calls += 1
        return await super().quality_counts(entry_ids)

    async def list_feedback_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, list[Feedback]]:
        self.calls += 1
        return await super().list_feedback_many(entry_ids, limit)

    async def entry_links_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, tuple[list[str], list[str]]]:
        self.calls += 1
        return await super().entry_links_many(entry_ids, limit)


async def test_a_batch_read_costs_the_same_store_reads_for_one_or_ten_entries() -> None:
    """ADR 0059: a batch read must not fan out one read per entry, or a
    single call holds a pod's whole connection pool."""
    store = _CountingStore(make_clock())
    target = await _write(store, READER, "the target")
    ids = [
        str(
            (
                await hive_write(
                    _surface(store, READER),
                    kind="fact",
                    summary=f"fact {i}",
                    body="b",
                    see_also=[target],
                )
            )["id"]
        )
        for i in range(MAX_GET_IDS - 1)
    ]
    for eid in ids:
        await hive_feedback(_surface(store, READER), eid, verdict="helpful", note="ok")

    costs = []
    for batch in ([ids[0]], [target, *ids]):
        store.calls = 0
        result = await hive_get(_surface(store, READER), entry_ids=batch)
        assert len(result["entries"]) == len(batch)  # type: ignore[arg-type]
        costs.append(store.calls)
    assert costs[0] == costs[1]

    # The batched answer matches the single reads, links and feedback included.
    entries = {e["id"]: e for e in result["entries"]}  # type: ignore[union-attr]
    for eid in (target, ids[0]):
        single = await hive_get(_surface(store, READER), eid)
        for key in ("feedback", "see_also", "linked_from"):
            assert entries[eid][key] == single[key], (eid, key)
