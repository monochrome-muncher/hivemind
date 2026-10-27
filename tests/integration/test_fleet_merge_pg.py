"""Fleet merge on real Postgres (ADR 0107): the race with in-flight writes,
undo after post-merge writes, and an agent reading its old fleet's entries
with the same key. Skips when Postgres is down, like the rest of the suite."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from tests.fakes import FakeEmbedder, make_search_config
from tests.pg import ensure_migrated

from hivemind.config import Settings
from hivemind.domain.access import FleetBusy, FleetMerged, TrustLevel
from hivemind.domain.audit import AuditFilters
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.fleet_merge import FleetMergeRefused
from hivemind.mcp.app import McpHivemind, hive_get, hive_list, hive_whoami, hive_write
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.keys import _issue_admin

TABLES = (
    "TRUNCATE audit_log, credentials, agents, fleets, entries, feedbacks, search_counts, "
    "entry_links, pins, fleet_merges, fleet_merge_entries"
)


@pytest.fixture
async def pool():
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    conn = await asyncpg.connect(dsn)
    await conn.execute(TABLES)
    await conn.close()
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    try:
        yield dsn, store, auth
    finally:
        await store.close()
        await auth.close()
        conn = await asyncpg.connect(dsn)
        await conn.execute(TABLES)
        await conn.close()


def _draft(fleet_id: str, summary: str, author: str = "amy") -> EntryDraft:
    return EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author=author,
        agent=author,
        scope="fleet",
        fleet_id=fleet_id,
    )


def _surface(store: PgStore, credential: Credential) -> McpHivemind:
    embedder = FakeEmbedder(dimension=Settings().embedding_dim)
    config = make_search_config()
    return McpHivemind(
        read_service=ReadService(store, GovernanceService(store), config),
        store=store,
        write_service=WriteService(store, embedder),
        search_service=SearchService(store, embedder, config),
        governance_service=GovernanceService(store),
        access_service=AccessService(store),
        search_config=config,
        credential=credential,
    )


async def _count(dsn: str, fleet_id: str) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        return int(
            await conn.fetchval("SELECT count(*) FROM entries WHERE fleet_id = $1", fleet_id)
        )
    finally:
        await conn.close()


async def test_a_write_that_resolved_the_source_before_the_merge_cannot_land_in_it(pool) -> None:
    dsn, store, _ = pool
    source, target = (await store.create_fleet("a")).id, (await store.create_fleet("b")).id
    stale = _draft(source, "resolved before the merge")  # the draft names the source fleet

    await store.merge_fleets(source, target_fleet_id=target, merged_by="admin:1")

    with pytest.raises(FleetMerged) as refused:
        await store.create_entry(stale, None)
    assert (refused.value.target_id, refused.value.target_name) == (target, "b")
    assert await _count(dsn, source) == 0


async def test_a_write_in_flight_across_the_merge_waits_and_is_then_refused(pool) -> None:
    dsn, store, _ = pool
    source, target = (await store.create_fleet("a")).id, (await store.create_fleet("b")).id
    # Stand in for a merge mid-flight: the source row locked and marked, not yet committed.
    merge = await asyncpg.connect(dsn)
    tx = merge.transaction()
    await tx.start()
    await merge.execute(
        "UPDATE fleets SET merged_into = $2, merged_at = now() WHERE id = $1", source, target
    )
    write = asyncio.create_task(store.create_entry(_draft(source, "in flight"), None))
    pin = asyncio.create_task(store.pin_entry(source, target, "lead", 10))
    await asyncio.sleep(0.4)
    assert not write.done() and not pin.done()  # both wait on the row lock
    await tx.commit()
    await merge.close()
    with pytest.raises(FleetMerged):
        await write
    with pytest.raises(FleetMerged):
        await pin
    assert await _count(dsn, source) == 0


async def test_a_write_gives_up_with_a_retryable_error_when_a_merge_holds_the_fleet_too_long(
    pool,
) -> None:
    dsn, store, _ = pool
    source, target = (await store.create_fleet("a")).id, (await store.create_fleet("b")).id
    merge = await asyncpg.connect(dsn)
    tx = merge.transaction()
    await tx.start()
    await merge.execute("SELECT 1 FROM fleets WHERE id = $1 FOR UPDATE", source)
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(FleetBusy, match="being merged or moved, retry"):
            await store.create_entry(_draft(source, "waits too long"), None)
        assert 2 < asyncio.get_running_loop().time() - started < 10
        with pytest.raises(TimeoutError):  # a 503 on every surface
            await store.pin_entry(source, target, "lead", 10)
    finally:
        await tx.rollback()
        await merge.close()
    assert await _count(dsn, source) == 0


async def test_a_merge_waits_for_a_write_in_flight_and_moves_it(pool) -> None:
    dsn, store, _ = pool
    source, target = (await store.create_fleet("a")).id, (await store.create_fleet("b")).id
    # A fleet write holds the share lock until it commits.
    writer = await asyncpg.connect(dsn)
    tx = writer.transaction()
    await tx.start()
    await writer.execute("SELECT 1 FROM fleets WHERE id = $1 FOR SHARE", source)
    await writer.execute(
        "INSERT INTO entries (kind, summary, author, agent, scope, fleet_id, occurred_at) "
        "VALUES ('fact', 'landing', 'amy', 'amy', 'fleet', $1, now())",
        source,
    )
    merge = asyncio.create_task(
        store.merge_fleets(source, target_fleet_id=target, merged_by="admin:1")
    )
    await asyncio.sleep(0.4)
    assert not merge.done()
    await tx.commit()
    await writer.close()
    outcome = await merge
    assert outcome.entries_moved == 1  # the write that won the race moved with the rest
    assert await _count(dsn, source) == 0 and await _count(dsn, target) == 1


async def test_writers_racing_a_merge_never_leave_an_entry_in_the_source(pool) -> None:
    dsn, store, _ = pool
    source, target = (await store.create_fleet("a")).id, (await store.create_fleet("b")).id

    async def write(i: int) -> str | None:
        try:
            await store.create_entry(_draft(source, f"w{i}"), None)
            return "ok"
        except FleetMerged:
            return "merged"

    writers = [asyncio.create_task(write(i)) for i in range(40)]
    await asyncio.sleep(0.02)
    outcome = await store.merge_fleets(source, target_fleet_id=target, merged_by="admin:1")
    results = await asyncio.gather(*writers)
    assert await _count(dsn, source) == 0
    assert await _count(dsn, target) == results.count("ok")  # every success moved with the merge
    assert outcome.entries_moved <= results.count("ok")


async def test_agents_read_their_old_fleet_with_the_same_key_after_the_merge(pool) -> None:
    dsn, store, auth = pool
    admin = await auth.verify(await _issue_admin(dsn, actor="test"))
    assert admin is not None
    access = AccessService(store, auth)
    a, b = await access.create_fleet("alpha", admin), await access.create_fleet("beta", admin)
    keys: dict[str, str] = {}
    for name, fleet in (("amy", a), ("bo", b)):
        await store.register_agent(name, "owner")
        _, keys[name] = await access.activate(name, TrustLevel.CONTRIBUTOR, fleet.id, admin)
    amy = await auth.verify(keys["amy"])
    assert amy is not None and amy.home_fleet_id == a.id
    old = await hive_write(_surface(store, amy), kind="fact", summary="alpha knowledge")
    bo = await auth.verify(keys["bo"])
    assert bo is not None
    await hive_write(_surface(store, bo), kind="fact", summary="beta knowledge")

    await access.merges.merge(a.id, admin, target_fleet_id=b.id)

    amy_again = await auth.verify(keys["amy"])  # the same key, no credential cache
    assert amy_again is not None and amy_again.home_fleet_id == b.id
    assert amy_again.trust_level is TrustLevel.CONTRIBUTOR
    surface = _surface(store, amy_again)
    assert (await hive_get(surface, entry_id=str(old["id"])))["summary"] == "alpha knowledge"
    listed = await hive_list(surface)
    assert {e["summary"] for e in listed["entries"]} == {"alpha knowledge", "beta knowledge"}  # type: ignore[attr-defined, union-attr]
    assert (await hive_whoami(surface))["home_fleet_name"] == "beta"
    written = await hive_write(surface, kind="fact", summary="after the merge")
    assert (await store.get_entry(str(written["id"]))).fleet_id == b.id  # type: ignore[union-attr]


async def test_undo_after_post_merge_writes_and_the_chained_refusal(pool) -> None:
    dsn, store, auth = pool
    admin = await auth.verify(await _issue_admin(dsn, actor="test"))
    assert admin is not None
    access = AccessService(store, auth)
    a, b = await access.create_fleet("alpha", admin), await access.create_fleet("beta", admin)
    await store.register_agent("amy")
    await access.activate("amy", TrustLevel.CONTRIBUTOR, a.id, admin)
    mine = await store.create_entry(_draft(a.id, "from alpha"), None)
    pinned = await store.create_entry(_draft(a.id, "pinned in alpha"), None)
    await store.pin_entry(a.id, pinned.id, "amy", 10)
    theirs = await store.create_entry(_draft(b.id, "from beta", "bo"), None)

    first = await access.merges.merge(a.id, admin, target_fleet_id=b.id)
    late = await store.create_entry(_draft(b.id, "written after"), None)
    chained = await access.merges.merge(b.id, admin, new_fleet_name="gamma")
    with pytest.raises(FleetMergeRefused):
        await access.merges.undo(first.record.id, admin)  # beta is merged since
    await access.merges.undo(chained.record.id, admin)

    undone = await access.merges.undo(first.record.id, admin)
    assert undone.entries_restored == 2 and undone.agents_restored == ("amy",)
    homes = {e.id: (await store.get_entry(e.id)).fleet_id for e in (mine, pinned, theirs, late)}  # type: ignore[union-attr]
    assert homes == {mine.id: a.id, pinned.id: a.id, theirs.id: b.id, late.id: b.id}
    assert [p.entry_id for p in await store.list_pins(a.id)] == [pinned.id]
    assert (await store.get_agent("amy")).home_fleet_id == a.id  # type: ignore[union-attr]
    listing = await access.merges.list_merges(admin)
    assert len(listing) == 2 and not any(v.undoable for v in listing)  # both are undone
    audits = {r.action.value for r in await store.list_audit(AuditFilters(), 100)}
    assert {"fleet.merge", "fleet.unmerge", "fleet.create"} <= audits
