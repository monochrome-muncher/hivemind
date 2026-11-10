"""Shared asyncpg pool construction. Each connection gets the pgvector codec and a jsonb codec with
a pass-through encoder, since ``PgStore`` already ``json.dumps`` (a real encoder would
double-encode).

``command_timeout`` is the client-side guard. ``statement_timeout`` is a server-side startup
parameter a transaction-mode PgBouncer rejects, so it is off (0) unless configured. ``migrate``
opens its own connections, so ``CREATE INDEX CONCURRENTLY`` is never cut short.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg
from pgvector.asyncpg import register_vector


@dataclass(frozen=True)
class PoolTimeouts:
    """Bounds for an app pool (``Settings.pool_*_timeout*``; 0 disables the server-side bound,
    ``None`` the client-side ones). ``acquire`` fails fast behind a saturated pool."""

    command: float | None = 30.0
    statement_ms: int = 0
    acquire: float | None = 10.0
    connect: float = 10.0


async def make_pool(
    dsn: str,
    min_size: int = 1,
    max_size: int = 10,
    *,
    command_timeout: float | None = 30.0,
    statement_timeout_ms: int = 0,
    connect_timeout: float = 10.0,
) -> asyncpg.Pool:
    """Build an asyncpg pool with the pgvector codec. ``statement_cache_size=0`` always: the
    prepared statement cache breaks behind a transaction-mode PgBouncer (``__asyncpg_stmt_N__ does
    not exist``)."""

    async def init(conn: asyncpg.Connection) -> None:
        await register_vector(conn)
        for typename in ("json", "jsonb"):
            await conn.set_type_codec(
                typename,
                encoder=_passthrough,
                decoder=json.loads,
                schema="pg_catalog",
            )

    extra: dict[str, Any] = {}
    if statement_timeout_ms > 0:
        extra["server_settings"] = {"statement_timeout": str(statement_timeout_ms)}
    return await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        init=init,
        statement_cache_size=0,
        command_timeout=command_timeout,
        timeout=connect_timeout,
        **extra,
    )


def _passthrough(value: str) -> str:
    return value
