"""A reader never gets a ``superseded_by`` it cannot open (ADR 0033): an
entry whose successor is out of the reader's sight keeps its ``superseded``
state but names no successor, on every read path of both surfaces."""

from __future__ import annotations

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import Entry, EntryDraft, EntryFilters, EntryState
from hivemind.mcp.app import McpHivemind, hive_get, hive_list
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
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


# A wrote E in fleet-1, then moved to fleet-2 (still reads E as its own);
# B in fleet-1 superseded E with S, which A cannot read.
A = _agent("alice", "fleet-2")
B = _agent("bob", "fleet-1")
C = _agent("carol", "fleet-1")


async def _world() -> tuple[MemoryStore, Entry, Entry, Entry]:
    store = MemoryStore(make_clock())

    async def put(author: str, summary: str, **kw: object) -> Entry:
        draft = EntryDraft(
            kind="fact",  # type: ignore[arg-type]
            summary=summary,
            author=author,
            agent=author,
            scope="fleet",
            fleet_id="fleet-1",
            **kw,  # type: ignore[arg-type]
        )
        return await store.create_entry(draft)

    old = await put("alice", "old take")
    mid = await put("bob", "newer take", supersedes=(old.id,))
    new = await put("bob", "newest take", supersedes=(mid.id,))
    return store, old, mid, new


def _read(store: MemoryStore) -> ReadService:
    return ReadService(store, GovernanceService(store), make_search_config())


async def test_a_hidden_successor_is_dropped_on_single_batch_and_list_reads() -> None:
    store, old, _, _ = await _world()
    read, visibility = _read(store), A.visibility()

    one = await read.entry(old.id, visibility=visibility)
    assert one is not None
    assert one.entry.state is EntryState.SUPERSEDED and one.entry.superseded_by is None

    batch = await read.entries([old.id], visibility=visibility)
    assert batch.entries[0].entry.state is EntryState.SUPERSEDED
    assert batch.entries[0].entry.superseded_by is None

    listed = await read.list(EntryFilters(include_inactive=True), visibility=visibility)
    assert [(e.id, e.superseded_by) for e in listed] == [(old.id, None)]


async def test_a_visible_successor_is_still_named() -> None:
    store, old, mid, _ = await _world()
    seen = await _read(store).entry(old.id, visibility=B.visibility())
    assert seen is not None and seen.entry.superseded_by == mid.id


async def test_history_entries_drop_hidden_successors_too() -> None:
    """Bob sees all three; a reader who sees old and mid but not new would
    get mid without the id of new."""
    store, old, mid, new = await _world()
    # Withdraw nothing: make ``new`` unreadable to the reader by scope.
    store._entries[new.id] = _with(store._entries[new.id], scope="self")
    reader = C.visibility()
    detail = await _read(store).entry(old.id, visibility=reader, include_history=True)
    assert detail is not None
    assert detail.entry.superseded_by == mid.id
    assert [e.id for e in detail.successors] == [mid.id]
    assert detail.successors[0].state is EntryState.SUPERSEDED
    assert detail.successors[0].superseded_by is None


async def test_one_batched_lookup_whatever_the_number_of_entries() -> None:
    store, _, _, _ = await _world()
    extra = [
        await store.create_entry(
            EntryDraft(
                kind="fact",  # type: ignore[arg-type]
                summary=f"x{i}",
                author="alice",
                agent="alice",
                scope="fleet",
                fleet_id="fleet-1",
            )
        )
        for i in range(3)
    ]
    for e in extra:
        await store.create_entry(
            EntryDraft(
                kind="fact",  # type: ignore[arg-type]
                summary=f"y{e.id}",
                author="bob",
                agent="bob",
                scope="fleet",
                fleet_id="fleet-1",
                supersedes=(e.id,),
            )
        )
    calls = 0
    original = store.get_entries

    async def counting(ids: list[str]) -> dict[str, Entry]:
        nonlocal calls
        calls += 1
        return await original(ids)

    store.get_entries = counting  # type: ignore[method-assign]
    listed = await _read(store).list(EntryFilters(include_inactive=True), visibility=A.visibility())
    assert len([e for e in listed if e.state is EntryState.SUPERSEDED]) == 4
    assert all(e.superseded_by is None for e in listed)
    assert calls == 1


def _with(entry: Entry, **changes: object) -> Entry:
    from dataclasses import replace

    return replace(entry, **changes)  # type: ignore[arg-type]


def _surface(store: MemoryStore, credential: Credential) -> McpHivemind:
    clock, embedder, config = make_clock(), make_embedder(), make_search_config()
    return McpHivemind(
        read_service=_read(store),
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        access_service=AccessService(store),
        search_config=config,
        credential=credential,
    )


async def test_mcp_get_and_list_drop_a_hidden_successor() -> None:
    store, old, _, _ = await _world()
    app = _surface(store, A)
    got = await hive_get(app, old.id)
    assert got["state"] == "superseded" and got["superseded_by"] is None
    many = await hive_get(app, entry_ids=[old.id])
    assert many["entries"][0]["superseded_by"] is None  # type: ignore[index]
    listed = await hive_list(app, include_inactive=True)
    assert [(i["id"], i["superseded_by"]) for i in listed["entries"]] == [(old.id, None)]  # type: ignore[attr-defined, index]
    seen_by_bob = await hive_get(_surface(store, B), old.id)
    assert seen_by_bob["superseded_by"] is not None


async def test_rest_get_batch_and_list_drop_a_hidden_successor() -> None:
    store, old, _, _ = await _world()
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"ka": A, "kb": B}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        ha, hb = {"X-API-Key": "ka"}, {"X-API-Key": "kb"}
        one = (await client.get(f"/v1/entries/{old.id}", headers=ha)).json()
        assert one["state"] == "superseded" and one["superseded_by"] is None
        batch = (
            await client.post("/v1/entries/get", json={"entry_ids": [old.id]}, headers=ha)
        ).json()
        assert batch["entries"][0]["superseded_by"] is None
        listed = (await client.get("/v1/entries?include_inactive=true", headers=ha)).json()
        assert [(e["id"], e["superseded_by"]) for e in listed] == [(old.id, None)]
        bobs = (await client.get(f"/v1/entries/{old.id}", headers=hb)).json()
        assert bobs["superseded_by"] is not None
