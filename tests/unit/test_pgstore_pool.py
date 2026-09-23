"""``PgStore`` threads its configured pool size to ``make_pool`` (the
gap: ``make_pool``'s ``min_size``/``max_size`` parameters existed but
nothing threaded a configured value into them). No live Postgres
needed — ``make_pool`` itself is spied on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from hivemind.store import build_store
from hivemind.store.pgstore import PgStore


async def test_pgstore_default_pool_size_matches_make_pool_defaults() -> None:
    store = PgStore("postgresql://example/db")
    with patch("hivemind.store.pgstore.make_pool", new=AsyncMock()) as make_pool:
        await store._ensure_pool()
    make_pool.assert_awaited_once_with("postgresql://example/db", min_size=1, max_size=10)


async def test_pgstore_configured_pool_size_reaches_make_pool() -> None:
    store = PgStore("postgresql://example/db", pool_min_size=2, pool_max_size=17)
    with patch("hivemind.store.pgstore.make_pool", new=AsyncMock()) as make_pool:
        await store._ensure_pool()
    make_pool.assert_awaited_once_with("postgresql://example/db", min_size=2, max_size=17)


async def test_build_store_threads_settings_pool_sizes() -> None:
    """``build_store`` (the REST + both MCP runners' seam) passes
    ``Settings.pool_min_size``/``pool_max_size`` through to the
    ``PgStore`` it constructs."""
    from hivemind.config import Settings

    settings = Settings(pool_min_size=4, pool_max_size=30)
    store = build_store(settings)
    assert isinstance(store, PgStore)
    with patch("hivemind.store.pgstore.make_pool", new=AsyncMock()) as make_pool:
        await store._ensure_pool()
    make_pool.assert_awaited_once_with(settings.database_url, min_size=4, max_size=30)
