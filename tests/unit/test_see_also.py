""" "See also" links between entries (ADR 0057): set on write, shown on
both ends of a read, limited to what the reader may see."""

from __future__ import annotations

from datetime import datetime

import httpx

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, EntryFilters
from hivemind.domain.validation import MAX_SEE_ALSO
from hivemind.mcp.app import McpHivemind, hive_get, hive_withdraw, hive_write
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.chain import MAX_LINKED_FROM
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


ALICE = _agent("alice", "fleet-a")
BOB = _agent("bob", "fleet-a")
CAROL = _agent("carol", "fleet-b")


def _surface(store: MemoryStore, credential: Credential) -> McpHivemind:
    clock, embedder, config = make_clock(), make_embedder(), make_search_config()
    return McpHivemind(
        read_service=ReadService(store, GovernanceService(store), config),
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config, now_fn=clock),
        governance_service=GovernanceService(store),
        access_service=AccessService(store),
        search_config=config,
        credential=credential,
    )


async def _write(store: MemoryStore, cred: Credential, summary: str, **kw: object) -> str:
    result = await hive_write(_surface(store, cred), kind="fact", summary=summary, **kw)  # type: ignore[arg-type]
    assert "error" not in result, result
    return str(result["id"])


def _ids(refs: object) -> list[str]:
    return [str(r["id"]) for r in refs]  # type: ignore[attr-defined, index]


async def test_links_show_on_both_ends() -> None:
    store = MemoryStore(make_clock())
    target = await _write(store, ALICE, "billing deploys need the kaniko image")
    linker = await _write(store, BOB, "billing runner moved to the new cluster", see_also=[target])
    alice = _surface(store, ALICE)

    from_linker = await hive_get(alice, linker)
    assert _ids(from_linker["see_also"]) == [target]
    assert from_linker["see_also"][0]["state"] == "active"  # type: ignore[index]
    assert from_linker["linked_from"] == []

    from_target = await hive_get(alice, target)
    assert from_target["see_also"] == []
    assert _ids(from_target["linked_from"]) == [linker]


async def test_writer_must_be_able_to_read_every_target() -> None:
    """An id the writer cannot read is rejected like an unknown one, so a
    link cannot be used to probe for entries in another fleet."""
    store = MemoryStore(make_clock())
    other_fleet = await _write(store, CAROL, "fleet-b internal note")
    result = await hive_write(
        _surface(store, ALICE), kind="fact", summary="a note", see_also=[other_fleet]
    )
    assert result["error"]["code"] == "invalid_input"  # type: ignore[index]
    assert "unknown see_also entries" in result["error"]["message"]  # type: ignore[index]
    unknown = await hive_write(
        _surface(store, ALICE),
        kind="fact",
        summary="a note",
        see_also=["00000000-0000-0000-0000-000000000000"],
    )
    assert unknown["error"]["code"] == "invalid_input"  # type: ignore[index]
    assert await store.count_entries(EntryFilters()) == 1  # nothing was written


async def test_readers_see_only_links_they_may_read() -> None:
    """A fleet-b entry links to an org-wide one: a fleet-a reader of the
    org entry does not learn that the fleet-b entry exists."""
    store = MemoryStore(make_clock())
    org_entry = await store.create_entry(
        EntryDraft(kind="fact", summary="org-wide fact", author="admin", agent="admin", scope="org")
    )
    await _write(store, CAROL, "fleet-b builds on the org fact", see_also=[org_entry.id])
    seen = await hive_get(_surface(store, ALICE), org_entry.id)
    assert seen["linked_from"] == []
    seen_by_carol = await hive_get(_surface(store, CAROL), org_entry.id)
    assert len(seen_by_carol["linked_from"]) == 1  # type: ignore[arg-type]


async def test_linked_from_lists_active_entries_only() -> None:
    store = MemoryStore(make_clock())
    target = await _write(store, ALICE, "the target")
    linker = await _write(store, ALICE, "the linker", see_also=[target])
    await hive_withdraw(_surface(store, ALICE), linker, reason="wrong")
    result = await hive_get(_surface(store, ALICE), target)
    assert result["linked_from"] == []
    # The withdrawn linker still shows its own outgoing link.
    withdrawn = await hive_get(_surface(store, ALICE), linker)
    assert _ids(withdrawn["see_also"]) == [target]


async def test_see_also_is_bounded_and_deduplicated() -> None:
    store = MemoryStore(make_clock())
    targets = [await _write(store, ALICE, f"fact {i}") for i in range(MAX_SEE_ALSO + 1)]
    too_many = await hive_write(
        _surface(store, ALICE), kind="fact", summary="too many", see_also=targets
    )
    assert too_many["error"]["code"] == "invalid_input"  # type: ignore[index]
    linker = await _write(store, ALICE, "dupes", see_also=[targets[0], targets[0].upper()])
    assert _ids((await hive_get(_surface(store, ALICE), linker))["see_also"]) == [targets[0]]


async def test_linked_from_is_capped_newest_first() -> None:
    # Distinct write times, as in real time; one-instant ties order by id.
    clock = make_clock()

    def ticking() -> datetime:
        clock.advance_days(1 / 86400)
        return clock()

    store = MemoryStore(ticking)
    target = await _write(store, ALICE, "popular")
    linkers = [
        await _write(store, ALICE, f"linker {i}", see_also=[target])
        for i in range(MAX_LINKED_FROM + 2)
    ]
    result = await hive_get(_surface(store, ALICE), target)
    assert _ids(result["linked_from"]) == list(reversed(linkers))[:MAX_LINKED_FROM]


async def test_unreadable_and_withdrawn_links_do_not_crowd_out_a_visible_one() -> None:
    """The cap counts only links the reader can see, so newer links from
    another fleet or from withdrawn entries leave the visible one in."""
    store = MemoryStore(make_clock())
    target = await store.create_entry(
        EntryDraft(kind="fact", summary="org-wide fact", author="admin", agent="admin", scope="org")
    )
    visible = await _write(store, ALICE, "visible linker", see_also=[target.id])
    for i in range(MAX_LINKED_FROM):
        await _write(store, CAROL, f"fleet-b linker {i}", see_also=[target.id])
    withdrawn = await _write(store, ALICE, "withdrawn linker", see_also=[target.id])
    await hive_withdraw(_surface(store, ALICE), withdrawn, reason="wrong")
    for i in range(MAX_LINKED_FROM):
        await _write(store, CAROL, f"carol self {i}", see_also=[target.id], scope="self")
    alice = _surface(store, ALICE)
    assert _ids((await hive_get(alice, target.id))["linked_from"])[-1] == visible
    batch = await hive_get(alice, entry_ids=[target.id])
    assert visible in _ids(batch["entries"][0]["linked_from"])  # type: ignore[index]


async def test_rest_write_and_reads_carry_links() -> None:
    store = MemoryStore(make_clock())
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials={"k": ALICE}),
        search_config=make_search_config(),
    )
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://t", headers={"X-API-Key": "k"}
    ) as client:
        target = (await client.post("/v1/entries", json={"kind": "fact", "summary": "t"})).json()
        linker = await client.post(
            "/v1/entries", json={"kind": "fact", "summary": "l", "see_also": [target["id"]]}
        )
        assert linker.status_code == 201, linker.text
        bad = await client.post(
            "/v1/entries", json={"kind": "fact", "summary": "x", "see_also": ["nope"]}
        )
        one = (await client.get(f"/v1/entries/{target['id']}")).json()
        many = (
            await client.post("/v1/entries/get", json={"entry_ids": [linker.json()["id"]]})
        ).json()
    assert bad.status_code == 422
    assert [link["id"] for link in one["linked_from"]] == [linker.json()["id"]]
    assert [link["id"] for link in many["entries"][0]["see_also"]] == [target["id"]]


async def test_any_uuid_spelling_names_the_target() -> None:
    """Upper case and no hyphens name an entry for ``see_also`` and a batch
    read as they do for a single read and ``supersedes`` (a hyphen-less id
    used to be "unknown see_also entries")."""
    store = MemoryStore(make_clock())
    target = await _write(store, ALICE, "billing deploys need the kaniko image")
    bare = target.replace("-", "")
    linker = await _write(store, BOB, "billing runner moved", see_also=[bare, target.upper()])
    alice = _surface(store, ALICE)
    assert _ids((await hive_get(alice, linker))["see_also"]) == [target]
    assert _ids((await hive_get(alice, bare))["linked_from"]) == [linker]
    batch = await hive_get(alice, entry_ids=[bare, target.upper()])
    assert batch["not_found"] == []
    assert [e["id"] for e in batch["entries"]] == [target]  # type: ignore[index]
