"""Shared asyncpg pool construction for the Postgres adapters.

Every connection registers the pgvector ``vector`` codec and a jsonb
codec: ``json.loads`` to decode, and a pass-through encoder because
``PgStore`` already ``json.dumps`` its values (a real encoder would
double-encode). Both are client-side, so safe behind PgBouncer.

**Timeouts.** ``command_timeout`` is the default client-side guard
(asyncpg cancels an overrunning call). ``statement_timeout`` is an
opt-in server-side startup parameter that a transaction-mode PgBouncer
rejects, so it is off (0) unless configured. ``migrate`` opens its own
connections, so ``CREATE INDEX CONCURRENTLY`` is never cut short.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg
from pgvector.asyncpg import register_vector


@dataclass(frozen=True)
class PoolTimeouts:
    """Bounds for an app pool (``Settings.pool_*_timeout*``; 0 disables
    the server-side statement bound, ``None`` the client-side ones).

    ``acquire`` is how long a caller waits for a free pooled connection
    before failing fast instead of hanging behind a saturated pool.
    """

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
    """Build an asyncpg pool whose connections speak the pgvector codec.

    ``statement_cache_size=0`` unconditionally: asyncpg's prepared
    statement cache is documented as unsafe behind a transaction-mode
    PgBouncer (``prepared statement "__asyncpg_stmt_N__" does not exist``
    under load), which is how the org runs Postgres.
    """

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
