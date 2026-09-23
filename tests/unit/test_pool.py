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

from typing import Any
from unittest.mock import AsyncMock, patch

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


async def test_configured_pool_sizes_reach_create_pool() -> None:
    """A caller-supplied ``min_size``/``max_size`` (e.g. from
    ``Settings.pool_min_size``/``pool_max_size``) reaches
    ``asyncpg.create_pool`` unchanged."""
    with patch("hivemind.store.pool.asyncpg.create_pool", new=AsyncMock()) as create_pool:
        await make_pool("postgresql://example/db", min_size=3, max_size=25)
    assert create_pool.call_args.kwargs["min_size"] == 3
    assert create_pool.call_args.kwargs["max_size"] == 25


async def test_init_callback_registers_the_vector_codec() -> None:
    """``make_pool`` still wires up the pgvector codec registration on
    every connection (unchanged by the statement-cache fix)."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch("hivemind.store.pool.asyncpg.create_pool", new=fake_create_pool):
        await make_pool("postgresql://example/db")
    assert callable(captured["init"])
