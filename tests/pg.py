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

import asyncpg
import pytest

from hivemind.store import migrate as migrate_module

_migrated: set[tuple[str, int]] = set()


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
