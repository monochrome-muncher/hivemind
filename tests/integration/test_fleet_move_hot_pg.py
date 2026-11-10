"""Fleet merge, undo and move stay HOT updates on real Postgres (ADR 0126).

The speed of those three statements depends on `UPDATE entries SET fleet_id`
being a heap-only update: no index may cover `fleet_id` and `entries` keeps
free room on each page. Adding such an index later would make a merge
re-insert every moved row into the HNSW index (minutes on a large fleet)
without failing any functional test, so this pins it. Skips when Postgres is
down, like the rest of the suite."""

from __future__ import annotations

import asyncpg
import pytest
from tests.pg import ensure_migrated, reset_tables

from hivemind.config import Settings
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.store import PgStore
from hivemind.store.pgstore import fleet_merge as fleet_merge_sql

ENTRIES = 6


@pytest.fixture
async def pool():
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    await reset_tables(dsn)
    store = PgStore(dsn)
    try:
        yield dsn, store
    finally:
        await store.close()


def _draft(fleet_id: str, i: int) -> EntryDraft:
    return EntryDraft(
        kind=Kind.FACT,
        summary=f"memory {i}",
        author="amy",
        agent="amy",
        scope="fleet",
        fleet_id=fleet_id,
    )


async def test_no_index_on_entries_covers_fleet_id(pool) -> None:
    dsn, _ = pool
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT c.relname AS name, pg_get_indexdef(i.indexrelid) AS definition,
                   (i.indkey::int2[] @> ARRAY[a.attnum]) AS on_column,
                   coalesce(pg_get_expr(i.indexprs, i.indrelid), '') AS exprs,
                   coalesce(pg_get_expr(i.indpred, i.indrelid), '') AS predicate
              FROM pg_index i
              JOIN pg_class c ON c.oid = i.indexrelid
              JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attname = 'fleet_id'
             WHERE i.indrelid = 'public.entries'::regclass
            """
        )
    finally:
        await conn.close()
    assert rows, "entries has indexes"  # the query matched something
    covering = [
        r["name"]
        for r in rows
        if r["on_column"]
        or "fleet_id" in r["exprs"]
        or "fleet_id" in r["predicate"]
        or "fleet_id" in r["definition"]
    ]
    assert covering == [], (
        f"{covering} cover entries.fleet_id, so a fleet merge, undo or move can no "
        "longer be a HOT update and re-inserts every moved row into the vector index (ADR 0126)"
    )


async def test_entries_keeps_the_free_room_a_hot_update_needs(pool) -> None:
    dsn, _ = pool
    conn = await asyncpg.connect(dsn)
    try:
        options = await conn.fetchval(
            "SELECT reloptions FROM pg_class WHERE oid = 'public.entries'::regclass"
        )
    finally:
        await conn.close()
    assert "fillfactor=50" in (options or [])


async def test_the_merge_undo_and_move_statements_are_hot_updates(pool) -> None:
    """Run the store's own statements on a connection of the test's, inside a
    transaction that is rolled back, and read `pg_stat_xact_user_tables`,
    which counts this transaction's updates at once. (The cumulative
    `pg_stat_user_tables` counters of the store's pooled connections are
    flushed later and on a timer, which would make this flaky.) The baseline
    is read inside each transaction, right before the statement: on PG15+
    the view includes the backend's pending counts, which reset when the
    backend flushes its stats while idle, so a delta across transactions
    can go negative.)"""
    dsn, store = pool
    source, target = (await store.create_fleet("a")).id, (await store.create_fleet("b")).id
    ids = [(await store.create_entry(_draft(source, i), None)).id for i in range(ENTRIES)]
    merge_id = (
        await store.merge_fleets(source, target_fleet_id=target, merged_by="admin:1")
    ).record.id

    async def counts(conn: asyncpg.Connection) -> tuple[int, int]:
        """This backend's (updates, HOT updates) on `entries` so far."""
        row = await conn.fetchrow(
            "SELECT n_tup_upd, n_tup_hot_upd FROM pg_stat_xact_user_tables "
            "WHERE relid = 'public.entries'::regclass"
        )
        # No row yet when the transaction has touched nothing.
        return (int(row["n_tup_upd"]), int(row["n_tup_hot_upd"])) if row else (0, 0)

    async def tidy(conn: asyncpg.Connection) -> None:
        """Reclaim the dead versions the earlier steps left, so each step starts
        with the room a quiet table has; a HOT update can use only free space
        on its own page, and pruning is otherwise left to chance."""
        await conn.execute("VACUUM entries")

    async def delta(conn: asyncpg.Connection, before: tuple[int, int]) -> tuple[int, int]:
        now = await counts(conn)
        return now[0] - before[0], now[1] - before[1]

    conn = await asyncpg.connect(dsn)
    try:
        # The merge above moved every entry to the target. Undo it ...
        await tidy(conn)
        tx = conn.transaction()
        await tx.start()
        try:
            before = await counts(conn)
            restored = await conn.fetch(fleet_merge_sql.RESTORE_ENTRIES, merge_id, source, target)
            assert len(restored) == ENTRIES
            assert await delta(conn, before) == (ENTRIES, ENTRIES)
        finally:
            await tx.rollback()

        # ... merge again with the merge statement ...
        await tidy(conn)
        tx = conn.transaction()
        await tx.start()
        try:
            before = await counts(conn)
            moved = await conn.fetch(fleet_merge_sql.MOVE_ENTRIES, target, source)
            assert len(moved) == ENTRIES
            assert await delta(conn, before) == (ENTRIES, ENTRIES)
        finally:
            await tx.rollback()

        # ... and move a chosen few, as a fleet move does.
        await tidy(conn)
        tx = conn.transaction()
        await tx.start()
        try:
            before = await counts(conn)
            few = await conn.fetch(
                fleet_merge_sql.MOVE_THESE_ENTRIES, target, source, list(ids[:2])
            )
            assert len(few) == 2
            assert await delta(conn, before) == (2, 2)
        finally:
            await tx.rollback()
    finally:
        await conn.close()
