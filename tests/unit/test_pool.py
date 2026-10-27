"""Tests for the asyncpg pool factory (``store/pool.py``).

The org runs a transaction-mode PgBouncer in front of Postgres, which
is documented to be incompatible with asyncpg's default client-side
cache of server-side prepared statements (stale "this statement is
prepared on connection X" bookkeeping once consecutive queries from one
client can land on different physical connections — surfacing as
``prepared statement "__asyncpg_stmt_N__" does not exist`` under
concurrent load). ``make_pool`` disables that cache unconditionally.

These tests spy on ``asyncpg.create_pool`` — no live Postgres needed.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hivemind.config import Settings
from hivemind.store.pool import make_pool


async def test_statement_cache_is_disabled() -> None:
    """``statement_cache_size=0`` reaches ``asyncpg.create_pool`` — the
    documented transaction-mode-PgBouncer mitigation."""
    with patch("hivemind.store.pool.asyncpg.create_pool", new=AsyncMock()) as create_pool:
        await make_pool("postgresql://example/db")
    assert create_pool.call_args.kwargs["statement_cache_size"] == 0


async def test_default_pool_sizes_reach_create_pool() -> None:
    with patch("hivemind.store.pool.asyncpg.create_pool", new=AsyncMock()) as create_pool:
        await make_pool("postgresql://example/db")
    assert create_pool.call_args.kwargs["min_size"] == 1
    assert create_pool.call_args.kwargs["max_size"] == 10


def test_a_bare_settings_gives_the_shipped_pool_sizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The per-pod pool a deployment gets when it sets nothing, and the same
    sizes ``make_pool`` falls back to."""
    for name in ("HIVEMIND_POOL_MIN_SIZE", "HIVEMIND_POOL_MAX_SIZE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)  # no .env.local to read
    settings = Settings()
    assert (settings.pool_min_size, settings.pool_max_size) == (1, 10)
    defaults = inspect.signature(make_pool).parameters
    assert (defaults["min_size"].default, defaults["max_size"].default) == (1, 10)


async def test_configured_pool_sizes_reach_create_pool() -> None:
    """A caller-supplied ``min_size``/``max_size`` (e.g. from
    ``Settings.pool_min_size``/``pool_max_size``) reaches
    ``asyncpg.create_pool`` unchanged."""
    with patch("hivemind.store.pool.asyncpg.create_pool", new=AsyncMock()) as create_pool:
        await make_pool("postgresql://example/db", min_size=3, max_size=25)
    assert create_pool.call_args.kwargs["min_size"] == 3
    assert create_pool.call_args.kwargs["max_size"] == 25
