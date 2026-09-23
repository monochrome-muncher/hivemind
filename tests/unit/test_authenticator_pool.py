"""``PgAuthenticator`` threads its configured pool size to ``make_pool``,
mirroring ``PgStore`` (``tests/unit/test_pgstore_pool.py``).

``PgAuthenticator`` runs its own, separate pool — auth is checked on
every authenticated request, on a connection lane distinct from
``PgStore``'s — so a pod's total Postgres connection footprint is the
sum of both pools. Before this it was hard-coded to ``make_pool``'s
bare 1/10 defaults regardless of ``Settings.pool_min_size`` /
``pool_max_size``, so an operator sizing one pool was silently not
sizing the other. No live Postgres needed — ``make_pool`` itself is
spied on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from hivemind.store import build_authenticator
from hivemind.store.auth import PgAuthenticator


async def test_authenticator_default_pool_size_matches_make_pool_defaults() -> None:
    auth = PgAuthenticator("postgresql://example/db")
    with patch("hivemind.store.auth.make_pool", new=AsyncMock()) as make_pool:
        await auth._ensure_pool()
    make_pool.assert_awaited_once_with("postgresql://example/db", min_size=1, max_size=10)


async def test_authenticator_configured_pool_size_reaches_make_pool() -> None:
    auth = PgAuthenticator("postgresql://example/db", pool_min_size=2, pool_max_size=17)
    with patch("hivemind.store.auth.make_pool", new=AsyncMock()) as make_pool:
        await auth._ensure_pool()
    make_pool.assert_awaited_once_with("postgresql://example/db", min_size=2, max_size=17)


async def test_build_authenticator_threads_settings_pool_sizes() -> None:
    """``build_authenticator`` (the REST + both MCP runners' seam) passes
    ``Settings.pool_min_size``/``pool_max_size`` through, the same as
    ``build_store`` does for ``PgStore``."""
    from hivemind.config import Settings

    settings = Settings(pool_min_size=4, pool_max_size=30)
    auth = build_authenticator(settings)
    assert isinstance(auth, PgAuthenticator)
    with patch("hivemind.store.auth.make_pool", new=AsyncMock()) as make_pool:
        await auth._ensure_pool()
    make_pool.assert_awaited_once_with(settings.database_url, min_size=4, max_size=30)
