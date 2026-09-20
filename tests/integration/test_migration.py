"""Postgres-backed integration test for the forward-migration path
(ADR 0013): the idempotent re-apply + ``schema_migrations`` version
tracking.

Validates that ``migrate`` (a) is idempotent (re-running is a no-op),
(b) records the applied schema generation in ``schema_migrations``, and
(c) ``current_schema_version`` reports the applied generation (or
``None`` for an un-migrated pool). Skips cleanly when Postgres is
unreachable.
"""

from __future__ import annotations

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.store.migrate import (
    SCHEMA_VERSION,
    current_embedding_dim,
    current_schema_version,
    migrate,
)


def _dsn() -> str:
    return Settings().database_url


async def _drop_schema_migrations(dsn: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DROP TABLE IF EXISTS schema_migrations CASCADE")
    finally:
        await conn.close()


async def test_migrate_records_schema_version() -> None:
    dsn = _dsn()
    dim = Settings().embedding_dim
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()

    await _drop_schema_migrations(dsn)
    # A fresh migrate creates the schema + records the version.
    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == SCHEMA_VERSION

    # Re-running migrate is idempotent: the version is unchanged (no
    # error, no duplicate row).
    await migrate(dsn, dim)
    assert await current_schema_version(dsn) == SCHEMA_VERSION

    # The schema_migrations table carries exactly one row (the upsert,
    # not an insert-per-run).
    conn = await asyncpg.connect(dsn)
    try:
        count = await conn.fetchval("SELECT count(*) FROM schema_migrations")
        assert count == 1
    finally:
        await conn.close()


async def test_current_schema_version_is_none_when_unmigrated() -> None:
    dsn = _dsn()
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()

    # Drop the tracking table -> current_schema_version reports None
    # (an un-migrated / pre-ADR-0013 pool).
    await _drop_schema_migrations(dsn)
    assert await current_schema_version(dsn) is None


async def test_migrate_fails_loudly_on_dim_mismatch() -> None:
    """ADR 0015: a pool provisioned at a different dim is a deployment
    error. ``migrate`` must fail *at migrate time* with the actionable
    message (both dims named + both remediations), not silently no-op
    and fail later with a confusing ``DataError`` on the first write.
    """
    dsn = _dsn()
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()

    pool_dim = await current_embedding_dim(dsn)
    if pool_dim is None:
        pytest.skip("pool has never been migrated — no dim to compare")

    # A dim guaranteed to differ from the pool's (the guard must fire).
    other_dim = 1024 if pool_dim != 1024 else 512
    with pytest.raises(RuntimeError) as excinfo:
        await migrate(dsn, other_dim)
    message = str(excinfo.value)
    # Both dims are named…
    assert f"{pool_dim}-dim" in message
    assert str(other_dim) in message
    # …and both remediations are offered.
    assert "HIVEMIND_EMBEDDING_DIM" in message
    assert "pg-reset" in message

    # The guard fired *before* applying anything: the pool's dim is
    # unchanged (no partial state from a failed migrate).
    assert await current_embedding_dim(dsn) == pool_dim
