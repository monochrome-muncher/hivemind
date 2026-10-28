"""Fleet move on real Postgres (ADR 0112): the races with in-flight writes and
home-fleet changes, the agents reading what they wrote through the same key,
and undo after later writes. Skips when Postgres is down, like the rest."""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from tests.fakes import FakeEmbedder, make_search_config
from tests.pg import ensure_migrated

from hivemind.config import Settings
from hivemind.domain.access import FleetBusy, TrustLevel
from hivemind.domain.audit import AuditFilters
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.fleet_merge import FleetMoveRefused
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


def _draft(fleet_id: str, summary: str, author: str) -> EntryDraft:
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


async def _count(dsn: str, fleet_id: str, author: str | None = None) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        if author is None:
            return int(
                await conn.fetchval("SELECT count(*) FROM entries WHERE fleet_id = $1", fleet_id)
            )
        return int(
            await conn.fetchval(
                "SELECT count(*) FROM entries WHERE fleet_id = $1 AND author = $2", fleet_id, author
            )
        )
    finally:
        await conn.close()


async def _fleets_and_agents(store: PgStore) -> tuple[str, str]:
    source, target = (await store.create_fleet("a")).id, (await store.create_fleet("b")).id
    for name in ("amy", "bob"):
        await store.register_agent(name)
        await store.activate_agent(name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=source)
    return source, target


async def test_a_write_that_resolved_the_old_home_before_the_move_cannot_land_in_the_source(
    pool,
) -> None:
    dsn, store, _ = pool
    source, target = await _fleets_and_agents(store)
    stale = _draft(source, "resolved before the move", "amy")  # names the old home fleet

    await store.move_agents(source, ["amy"], target_fleet_id=target, moved_by="admin:1")

    with pytest.raises(FleetBusy, match="home fleet of amy changed"):
        await store.create_entry(stale, None)
    assert await _count(dsn, source) == 0
    # The stayer's writes into the source go on as before.
    await store.create_entry(_draft(source, "bob goes on", "bob"), None)
    assert await _count(dsn, source, "bob") == 1


async def test_a_write_in_flight_across_the_move_waits_and_is_then_refused(pool) -> None:
    dsn, store, _ = pool
    source, target = await _fleets_and_agents(store)
    # Stand in for a move mid-flight: both fleet rows locked and amy re-homed, not committed.
    move = await asyncpg.connect(dsn)
    tx = move.transaction()
    await tx.start()
    await move.execute(
        "SELECT 1 FROM fleets WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE", [source, target]
    )
    await move.execute("UPDATE agents SET home_fleet_id = $1 WHERE name = 'amy'", target)
    write = asyncio.create_task(store.create_entry(_draft(source, "in flight", "amy"), None))
    stayer = asyncio.create_task(store.create_entry(_draft(source, "bob in flight", "bob"), None))
    await asyncio.sleep(0.4)
    assert not write.done() and not stayer.done()  # both wait on the source row
    await tx.commit()
    await move.close()
    with pytest.raises(FleetBusy):
        await write
    await stayer  # bob did not move: his write lands in the source
    assert await _count(dsn, source, "amy") == 0 and await _count(dsn, source, "bob") == 1


async def test_a_write_into_the_target_waits_for_the_move_too(pool) -> None:
    dsn, store, _ = pool
    _, target = await _fleets_and_agents(store)
    await store.register_agent("cat")
    await store.activate_agent("cat", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=target)
    move = await asyncpg.connect(dsn)
    tx = move.transaction()
    await tx.start()
    await move.execute("SELECT 1 FROM fleets WHERE id = $1 FOR UPDATE", target)
    try:
        with pytest.raises(FleetBusy):
            await store.create_entry(_draft(target, "waits too long", "cat"), None)
    finally:
        await tx.rollback()
        await move.close()
    assert await _count(dsn, target) == 0


async def test_a_move_waits_for_a_write_in_flight_and_moves_it(pool) -> None:
    dsn, store, _ = pool
    source, target = await _fleets_and_agents(store)
    writer = await asyncpg.connect(dsn)
    tx = writer.transaction()
    await tx.start()
    await writer.execute("SELECT 1 FROM fleets WHERE id = $1 FOR SHARE", source)
    await writer.execute(
        "INSERT INTO entries (kind, summary, author, agent, scope, fleet_id, occurred_at) "
        "VALUES ('fact', 'landing', 'amy', 'amy', 'fleet', $1, now())",
        source,
    )
    move = asyncio.create_task(
        store.move_agents(source, ["amy"], target_fleet_id=target, moved_by="admin:1")
    )
    await asyncio.sleep(0.4)
    assert not move.done()
    await tx.commit()
    await writer.close()
    outcome = await move
    assert outcome.entries_moved == 1  # the write that won the race moved with the rest
    assert await _count(dsn, source) == 0 and await _count(dsn, target) == 1


async def test_writers_racing_a_move_never_leave_a_mover_entry_in_the_source(pool) -> None:
    dsn, store, _ = pool
    source, target = await _fleets_and_agents(store)

    async def write(author: str, i: int) -> str:
        try:
            await store.create_entry(_draft(source, f"{author}{i}", author), None)
            return "ok"
        except FleetBusy:
            return "busy"

    writers = [asyncio.create_task(write("amy" if i % 2 else "bob", i)) for i in range(60)]
    await asyncio.sleep(0.02)
    outcome = await store.move_agents(source, ["amy"], target_fleet_id=target, moved_by="admin:1")
    results = await asyncio.gather(*writers)
    amy_ok = sum(1 for i, r in enumerate(results) if i % 2 and r == "ok")
    bob_ok = sum(1 for i, r in enumerate(results) if not i % 2 and r == "ok")
    assert await _count(dsn, source, "amy") == 0  # nothing of amy's is left behind
    assert await _count(dsn, target, "amy") == amy_ok  # every success moved with the move
    assert await _count(dsn, source, "bob") == bob_ok and bob_ok == 30  # bob never refused
    # Every write that succeeded landed before the move took its locks and moved with it;
    # the rest were refused.
    assert outcome.entries_moved == amy_ok


async def test_a_move_and_a_home_fleet_change_of_a_mover_never_disagree(pool) -> None:
    dsn, store, _ = pool
    source, target = await _fleets_and_agents(store)
    other = (await store.create_fleet("c")).id
    await store.create_entry(_draft(source, "amy's", "amy"), None)
    results = await asyncio.gather(
        store.move_agents(source, ["amy"], target_fleet_id=target, moved_by="admin:1"),
        store.set_agent_home_fleet("amy", other),
        return_exceptions=True,
    )
    home = (await store.get_agent("amy")).home_fleet_id  # type: ignore[union-attr]
    moved = not isinstance(results[0], BaseException)
    if moved:
        # The move won the agent row: the entry went with her, the PATCH came after.
        assert not isinstance(results[1], BaseException)
        assert home == other
        assert await _count(dsn, target, "amy") == 1 and await _count(dsn, source, "amy") == 0
    else:
        assert isinstance(results[0], FleetMoveRefused)  # the PATCH won: she is not in the source
        assert home == other and await _count(dsn, source, "amy") == 1
        assert await _count(dsn, target, "amy") == 0


async def test_agents_read_what_they_wrote_with_the_same_key_after_the_move(pool) -> None:
    dsn, store, auth = pool
    admin = await auth.verify(await _issue_admin(dsn, actor="test"))
    assert admin is not None
    access = AccessService(store, auth)
    a, b = await access.create_fleet("alpha", admin), await access.create_fleet("beta", admin)
    keys: dict[str, str] = {}
    for name, fleet in (("amy", a), ("ann", a), ("bo", b)):
        await store.register_agent(name, "owner")
        _, keys[name] = await access.activate(name, TrustLevel.CONTRIBUTOR, fleet.id, admin)
    amy = await auth.verify(keys["amy"])
    ann = await auth.verify(keys["ann"])
    assert amy is not None and ann is not None
    mine = await hive_write(_surface(store, amy), kind="fact", summary="amy's alpha knowledge")
    await hive_write(_surface(store, ann), kind="fact", summary="ann's alpha knowledge")

    out = await access.merges.move(a.id, ["amy"], admin, target_fleet_id=b.id)
    assert out.entries_moved == 1 and out.entries_held_back == 0

    amy_again = await auth.verify(keys["amy"])  # the same key, no credential cache
    assert amy_again is not None and amy_again.home_fleet_id == b.id
    surface = _surface(store, amy_again)
    assert (await hive_get(surface, entry_id=str(mine["id"])))["summary"] == "amy's alpha knowledge"
    listed = await hive_list(surface)
    assert {e["summary"] for e in listed["entries"]} == {"amy's alpha knowledge"}  # type: ignore[attr-defined, union-attr]
    assert (await hive_whoami(surface))["home_fleet_name"] == "beta"
    written = await hive_write(surface, kind="fact", summary="after the move")
    assert (await store.get_entry(str(written["id"]))).fleet_id == b.id  # type: ignore[union-attr]
    # The stayer no longer sees what amy wrote, and still sees her own.
    ann_again = await auth.verify(keys["ann"])
    assert ann_again is not None
    seen = await hive_list(_surface(store, ann_again))
    assert {e["summary"] for e in seen["entries"]} == {"ann's alpha knowledge"}  # type: ignore[attr-defined, union-attr]


async def test_undo_after_later_writes_and_the_audit_trail(pool) -> None:
    dsn, store, auth = pool
    admin = await auth.verify(await _issue_admin(dsn, actor="test"))
    assert admin is not None
    access = AccessService(store, auth)
    a, b = await access.create_fleet("alpha", admin), await access.create_fleet("beta", admin)
    for name in ("amy", "ann"):
        await store.register_agent(name)
        await access.activate(name, TrustLevel.CONTRIBUTOR, a.id, admin)
    v1 = await store.create_entry(_draft(a.id, "v1", "amy"), None)
    pinned = await store.create_entry(_draft(a.id, "pinned", "amy"), None)
    await store.pin_entry(a.id, pinned.id, "ann", 10)  # a stayer's pin on amy's memory
    anns = await store.create_entry(_draft(a.id, "ann's", "ann"), None)

    out = await access.merges.move(a.id, ["amy"], admin, target_fleet_id=b.id)
    late = await store.create_entry(_draft(b.id, "written after", "amy"), None)
    assert [p.entry_id for p in await store.list_pins(b.id)] == [pinned.id]

    undone = await access.merges.undo(out.record.id, admin)
    assert (undone.entries_restored, undone.agents_restored) == (2, ("amy",))
    homes = {e.id: (await store.get_entry(e.id)).fleet_id for e in (v1, pinned, anns, late)}  # type: ignore[union-attr]
    assert homes == {v1.id: a.id, pinned.id: a.id, anns.id: a.id, late.id: b.id}
    assert [p.entry_id for p in await store.list_pins(a.id)] == [pinned.id]
    assert (await store.get_agent("amy")).home_fleet_id == a.id  # type: ignore[union-attr]
    (view,) = await access.merges.list_merges(admin)
    assert view.record.kind == "move" and view.record.undone_at is not None and not view.undoable
    assert (await store.get_fleet(a.id)).merged_into is None  # type: ignore[union-attr]
    audits = await store.list_audit(AuditFilters(), 100)
    by_action = {r.action.value: r for r in audits}
    assert by_action["fleet.move_agents"].detail["entries"] == 2
    assert by_action["fleet.move_undo"].detail["move_id"] == out.record.id
    assert "fleet.merge" not in by_action and "fleet.unmerge" not in by_action
