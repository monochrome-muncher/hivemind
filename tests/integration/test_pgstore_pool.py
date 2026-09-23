"""Postgres-backed integration test for the pool-size settings threading
(the multi-replica-readiness pgbouncer fix).

What this test CAN prove: a ``PgStore`` built via ``build_store`` from a
``Settings`` with a small, distinctive ``pool_max_size``/``pool_min_size``
actually creates an underlying asyncpg ``Pool`` sized accordingly —
i.e. the value really travels ``Settings`` -> ``build_store`` ->
``PgStore.__init__`` -> ``PgStore._ensure_pool`` -> ``make_pool`` ->
``asyncpg.create_pool`` end to end, against a real Postgres.

What this test CANNOT prove: that the underlying prepared-statement
hazard (asyncpg's client-side statement cache going stale when
consecutive queries land on different physical connections behind a
*transaction-mode* PgBouncer) is actually fixed. There is no PgBouncer
in this test harness, and the dev Postgres here is talked to directly
(session-mode-equivalent: one asyncpg connection stays affiliated with
one query sequence), so the failure mode this whole task exists to fix
cannot be reproduced or disproven in-process. The ``statement_cache_size=0``
assertion (unit-tested in ``tests/unit/test_pool.py`` via a spy on
``asyncpg.create_pool``) is the closest hermetic proxy for that; this
test only proves the *pool sizing* half of the settings-threading gap
is closed against a real server.

Skips cleanly when Postgres is unreachable (the existing pattern in
this directory).
"""

from __future__ import annotations

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.store import PgStore, build_store


def _dsn() -> str:
    return Settings().database_url


@pytest.fixture(autouse=True)
async def _skip_if_postgres_down() -> None:
    dsn = _dsn()
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()


async def test_configured_pool_max_size_reaches_the_real_pool() -> None:
    settings = Settings(database_url=_dsn(), pool_min_size=1, pool_max_size=3)
    store = build_store(settings)
    assert isinstance(store, PgStore)
    try:
        pool = await store._ensure_pool()
        assert pool.get_max_size() == 3
        assert pool.get_min_size() == 1
    finally:
        await store.close()


async def test_default_pool_size_is_unchanged_when_settings_are_unconfigured() -> None:
    """A bare ``Settings()`` (no ``HIVEMIND_POOL_*`` override) still
    yields ``make_pool``'s own defaults (1/10) — this task must not
    re-tune the existing constant, only make it reachable."""
    settings = Settings(database_url=_dsn())
    store = build_store(settings)
    assert isinstance(store, PgStore)
    try:
        pool = await store._ensure_pool()
        assert pool.get_min_size() == 1
        assert pool.get_max_size() == 10
    finally:
        await store.close()
