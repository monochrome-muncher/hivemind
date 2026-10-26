"""A saturated pool surfaces as a builtin ``TimeoutError`` (ROADMAP 3.13).

The REST and MCP surfaces map ``TimeoutError`` to 503 / ``store_unavailable``;
this pins, on real Postgres, that an exhausted ``PgStore`` pool raises exactly
that type, so the mapping is not resting on an assumption about asyncpg.
Skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import pytest
from tests.pg import ensure_migrated

from hivemind.config import Settings
from hivemind.store import PgStore
from hivemind.store.pool import PoolTimeouts


async def test_an_exhausted_pool_raises_timeout_error() -> None:
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    store = PgStore(dsn, pool_min_size=1, pool_max_size=1, timeouts=PoolTimeouts(acquire=0.2))
    try:
        pool = await store._ensure_pool()
        async with pool.acquire():  # the only connection is taken
            with pytest.raises(TimeoutError):
                await store.get_agent("anyone")
        assert await store.get_agent("anyone") is None  # and recovers once freed
    finally:
        await store.close()
