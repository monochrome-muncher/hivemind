"""Postgres-backed integration tests for the migration chain (ADR 0020).

Covers the four properties the chain is relied on for:

* **forward** — a fresh pool applies the chain and reports its head;
* **idempotent** — re-running applies nothing (the entrypoint runs it on
  every pod start, ADR 0018);
* **concurrent** — many migrators racing a cold pool all succeed and the
  chain is applied exactly once (the advisory lock, ADR 0020);
* **loud on a dim mismatch** — the ADR 0015 guard still fires *before*
  any DDL.

Skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.store.migrate import (
    current_embedding_dim,
    current_schema_version,
    migrate,
    rollback,
)

HEAD = "0001.initial-schema"


def _dsn() -> str:
    return Settings().database_url


async def _require_postgres(dsn: str) -> None:
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()


async def _reset_chain(dsn: str) -> None:
    """Drop the whole public schema so the next migrate starts cold."""
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    finally:
        await conn.close()


async def test_migrate_applies_the_chain_and_reports_its_head() -> None:
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)

    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD

    # Re-running is a no-op: same head, still one row (the entrypoint
    # runs migrate on every pod start — ADR 0018).
    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == HEAD

    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM _yoyo_migration") == 1
    finally:
        await conn.close()


async def test_concurrent_migrators_are_serialised_by_the_advisory_lock() -> None:
    """ADR 0020: two projects x 2-3 replicas all migrate on start.

    Without the lock this is not merely theoretical — the first
    statement (`CREATE EXTENSION IF NOT EXISTS vector`) raises
    `UniqueViolation` for the losers, which under ADR 0018 exits the
    entrypoint before the runner execs.
    """
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)

    results = await asyncio.gather(*[migrate(dsn, dim) for _ in range(6)], return_exceptions=True)
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, f"concurrent migrators failed: {failures}"

    assert await current_schema_version(dsn) == HEAD
    conn = await asyncpg.connect(dsn)
    try:
        # Applied exactly once, not once per migrator.
        assert await conn.fetchval("SELECT count(*) FROM _yoyo_migration") == 1
        assert await conn.fetchval("SELECT to_regclass('entries') IS NOT NULL")
    finally:
        await conn.close()


async def test_migrate_releases_the_lock_for_the_next_caller() -> None:
    """The advisory lock must not leak: a second migrate in the same
    process (and a third from a fresh connection) must not block."""
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)

    await migrate(dsn, dim)
    await asyncio.wait_for(migrate(dsn, dim), timeout=30)

    conn = await asyncpg.connect(dsn)
    try:
        held = await conn.fetchval(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'",
        )
        assert held == 0, "advisory lock still held after migrate returned"
    finally:
        await conn.close()


async def test_current_schema_version_is_none_when_unmigrated() -> None:
    dsn = _dsn()
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    assert await current_schema_version(dsn) is None


async def test_rollback_refuses_to_drop_the_initial_schema() -> None:
    """ADR 0020: a rollback that would destroy data is not written.

    Rolling back `0001` drops every entry in the pool, so `rollback`
    refuses and names the two real remediations instead.
    """
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)

    with pytest.raises(RuntimeError) as excinfo:
        await rollback(dsn, dim, count=1)
    message = str(excinfo.value)
    assert "0001.initial-schema" in message
    assert "pg-reset" in message or "backup" in message

    # The refusal left the pool intact.
    assert await current_schema_version(dsn) == HEAD


async def test_migrate_fails_loudly_on_dim_mismatch() -> None:
    """ADR 0015: a pool provisioned at a different dim is a deployment
    error. The guard fires *before* the lock is taken and before any
    DDL, so there is no partial state and no lock to leak.
    """
    dsn, dim = _dsn(), Settings().embedding_dim
    await _require_postgres(dsn)
    await _reset_chain(dsn)
    await migrate(dsn, dim)

    pool_dim = await current_embedding_dim(dsn)
    assert pool_dim is not None
    other_dim = 1024 if pool_dim != 1024 else 512

    with pytest.raises(RuntimeError) as excinfo:
        await migrate(dsn, other_dim)
    message = str(excinfo.value)
    assert f"{pool_dim}-dim" in message
    assert str(other_dim) in message
    assert "HIVEMIND_EMBEDDING_DIM" in message
    assert "pg-reset" in message

    # No partial state, and the lock was never taken.
    assert await current_embedding_dim(dsn) == pool_dim
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") == 0
    finally:
        await conn.close()
