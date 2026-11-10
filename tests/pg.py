"""Bring the test database to the head of the migration chain, once per process.

Nearly every integration fixture used to call ``migrate`` for each test,
about 90 ms of lock, advisory-lock and index checks to learn that nothing
was outstanding. ``ensure_migrated`` runs the full ``migrate`` the first
time a process asks for a ``(dsn, dim)``; after that it only reads yoyo's
table, and migrates again if the schema is no longer at head or at the
dim. ``tests/integration/test_migration.py`` drops the schema and leaves
the pool at other revisions, so the memo alone would not be safe.

The memo is per process, so under pytest-xdist each worker (with its own
database) migrates once.

It also replaces the fixtures' separate reachability and ``vector``
probes with one connection, and skips with the messages
``tests/conftest.py`` ``_PG_SKIP_MARKERS`` turns into failures under
``HM_TEST_REQUIRE_PG=1``.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from hivemind.store import migrate as migrate_module

_migrated: set[tuple[str, int]] = set()
_tables: dict[str, list[str]] = {}


def _chain_ids() -> set[str]:
    """The shipped migration ids (a yoyo id is the file name without its
    extension; ``.rollback.sql`` files are not migrations)."""
    return {
        path.stem
        for path in migrate_module._MIGRATIONS_DIR.iterdir()
        if path.suffix in (".sql", ".py") and not path.name.endswith(".rollback.sql")
    }


async def _at_head(conn: asyncpg.Connection, dim: int) -> bool:
    try:
        rows = await conn.fetch("SELECT migration_id FROM _yoyo_migration")
        typmod = await conn.fetchval(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'entries'::regclass AND attname = 'embedding'"
        )
    except asyncpg.exceptions.UndefinedTableError:
        return False  # dropped, or never migrated
    return _chain_ids() <= {r["migration_id"] for r in rows} and typmod == dim


async def ensure_migrated(dsn: str, dim: int) -> None:
    """Skip when Postgres or pgvector is missing; otherwise leave the
    database migrated to head at ``dim``."""
    try:
        conn = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    try:
        if not await conn.fetchval(
            "SELECT count(*) FROM pg_available_extensions WHERE name = 'vector'"
        ):
            pytest.skip("'vector' extension is not available on the dev database")
        current = (dsn, dim) in _migrated and await _at_head(conn, dim)
    finally:
        await conn.close()
    if not current:
        await migrate_module.migrate(dsn, dim)
        _migrated.add((dsn, dim))
        _tables.pop(dsn, None)  # the schema may have been rebuilt


async def reset_tables(dsn: str) -> None:
    """Empty every application table, in one round trip. Call it once per
    test, before the test, after ``ensure_migrated``.

    The list comes from the schema (tables in ``public`` except yoyo's
    ``_yoyo_*`` and ``yoyo_lock``), so a table a later migration adds is
    never forgotten, and it is cached per ``dsn`` for the process. Rows are
    removed with ``DELETE``, children before the tables their foreign keys
    point at, in one transaction.

    Why ``DELETE`` and not ``TRUNCATE``: with fsync on (CI's default) a
    TRUNCATE of the 13 tables costs about 40 ms, because it rewrites the
    relation files and syncs them; the same tables emptied with DELETE cost
    about 1 ms. Over the suite's ~1000 fixtures (each used to truncate
    before and after) that is most of the Postgres tests' time. Sequences
    are not reset: TRUNCATE without ``RESTART IDENTITY`` did not reset them
    either. Dead tuples stay until autovacuum; the vector-index recall
    module, which measures the HNSW graph, keeps its own TRUNCATE.
    """
    conn = await asyncpg.connect(dsn)
    try:
        names = _tables.get(dsn)
        if names is None:
            names = await _fk_ordered_tables(conn)
            _tables[dsn] = names
        async with conn.transaction():
            await conn.execute("".join(f'DELETE FROM "{name}"; ' for name in names))
    finally:
        await conn.close()


async def wait_until_blocked(dsn: str, count: int = 1, *, timeout: float = 10.0) -> None:
    """Return once ``count`` sessions in this database are waiting on a lock.

    For the tests that hold a lock on one connection, start the operation
    under test as a task and assert it waits. A fixed sleep before
    ``assert not task.done()`` passes just as well when the task has not
    reached Postgres yet; this returns only when Postgres itself reports the
    waiters, and fails if they never block. Each xdist worker has its own
    database, so another worker's waiters are not counted.
    """
    conn = await asyncpg.connect(dsn)
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            waiting = await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            if waiting >= count:
                return
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"{waiting} session(s) waiting on a lock after {timeout} s, expected {count}"
                )
            await asyncio.sleep(0.005)
    finally:
        await conn.close()


async def _fk_ordered_tables(conn: asyncpg.Connection) -> list[str]:
    """Application tables, each before every table it references."""
    rows = await conn.fetch(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
        "AND c.relname NOT LIKE '\\_yoyo\\_%' AND c.relname <> 'yoyo_lock' "
        "ORDER BY c.relname"
    )
    remaining = {r["relname"] for r in rows}
    refs = await conn.fetch(
        "SELECT conrelid::regclass::text AS child, confrelid::regclass::text AS parent "
        "FROM pg_constraint WHERE contype = 'f' AND conrelid <> confrelid"
    )
    parents: dict[str, set[str]] = {name: set() for name in remaining}
    for r in refs:
        if r["child"] in parents and r["parent"] in remaining:
            parents[r["parent"]].add(r["child"])  # parent waits for its children
    ordered: list[str] = []
    while remaining:
        ready = sorted(n for n in remaining if not (parents[n] & remaining))
        if not ready:
            raise RuntimeError(f"foreign-key cycle among {sorted(remaining)}")
        ordered += ready
        remaining -= set(ready)
    return ordered
