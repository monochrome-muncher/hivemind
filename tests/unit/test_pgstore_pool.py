"""``PgStore`` and ``PgAuthenticator`` thread their configured pool size to
``make_pool`` (the gap: ``make_pool``'s ``min_size``/``max_size`` parameters
existed but nothing threaded a configured value into them).

The authenticator runs its own, separate pool, on a connection lane distinct
from the store's, so a pod's total Postgres connection footprint is the sum of
both pools. No live Postgres needed — ``make_pool`` itself is spied on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from hivemind.config import Settings
from hivemind.store import build_authenticator, build_store
from hivemind.store.auth import PgAuthenticator
from hivemind.store.pgstore import PgStore

POOLS = pytest.mark.parametrize(
    ("make_pool_path", "cls", "build"),
    [
        ("hivemind.store.pgstore.make_pool", PgStore, build_store),
        ("hivemind.store.auth.make_pool", PgAuthenticator, build_authenticator),
    ],
    ids=["PgStore", "PgAuthenticator"],
)


@POOLS
async def test_default_pool_size_matches_make_pool_defaults(make_pool_path, cls, build) -> None:
    owner = cls("postgresql://example/db")
    with patch(make_pool_path, new=AsyncMock()) as make_pool:
        await owner._ensure_pool()
    make_pool.assert_awaited_once()
    assert make_pool.await_args.args == ("postgresql://example/db",)
    assert make_pool.await_args.kwargs["min_size"] == 1
    assert make_pool.await_args.kwargs["max_size"] == 10


@POOLS
async def test_configured_pool_size_reaches_make_pool(make_pool_path, cls, build) -> None:
    owner = cls("postgresql://example/db", pool_min_size=2, pool_max_size=17)
    with patch(make_pool_path, new=AsyncMock()) as make_pool:
        await owner._ensure_pool()
    make_pool.assert_awaited_once()
    assert make_pool.await_args.args == ("postgresql://example/db",)
    assert make_pool.await_args.kwargs["min_size"] == 2
    assert make_pool.await_args.kwargs["max_size"] == 17


@POOLS
async def test_build_threads_settings_pool_sizes(make_pool_path, cls, build) -> None:
    """``build_store`` / ``build_authenticator`` (the REST + both MCP runners'
    seam) pass ``Settings.pool_min_size``/``pool_max_size`` through."""
    settings = Settings(pool_min_size=4, pool_max_size=30)
    owner = build(settings)
    assert isinstance(owner, cls)
    with patch(make_pool_path, new=AsyncMock()) as make_pool:
        await owner._ensure_pool()
    make_pool.assert_awaited_once()
    assert make_pool.await_args.args == (settings.database_url,)
    assert make_pool.await_args.kwargs["min_size"] == 4
    assert make_pool.await_args.kwargs["max_size"] == 30
