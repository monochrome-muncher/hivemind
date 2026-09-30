"""Shared asyncpg pool construction for the Postgres adapters.

One small factory keeps the pgvector type-codec registration in a
single place: every connection in the pool registers the ``vector``
codec (``pgvector.asyncpg.register_vector``) so that vector columns
round-trip as plain Python lists (in) and ``pgvector.Vector`` (out).

**jsonb codec.** asyncpg returns ``json``/``jsonb`` as JSON *text* unless a
codec is registered, so ``payload`` came back as a ``str`` (REST entry
responses failed validation; MCP returned a string). Every connection
registers a decoder (``json.loads``) and a *pass-through* encoder:
``PgStore`` already ``json.dumps`` its values and casts ``$n::jsonb``, so
an encoding codec would double-encode. The codec is client-side, hence
safe behind a transaction-mode PgBouncer.

**Timeouts.** ``command_timeout`` bounds every client-side call (asyncpg
sends a cancel request when a call overruns it, which a PgBouncer
forwards); it is the default guard. ``statement_timeout`` (a server
setting sent in the startup packet) is an opt-in extra bound for pools
that connect to Postgres directly: a transaction-mode PgBouncer rejects
unknown startup parameters by default (``unsupported startup parameter``)
and, even when told to ignore one, never applies it to its shared server
connections — so it is off (0) unless configured. Only app pools get
these bounds: ``migrate`` opens its own connections, so ``CREATE INDEX
CONCURRENTLY`` is never cut short.
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

    ``statement_cache_size=0`` disables asyncpg's client-side cache of
    server-side prepared statements. That cache is asyncpg's default
    (ON), and it is documented to be **unsafe behind a transaction-mode
    PgBouncer** (or any transaction-pooling proxy): a client's "this
    statement is prepared on connection X" bookkeeping goes stale the
    moment consecutive queries from that client land on different
    physical server connections, surfacing as ``prepared statement
    "__asyncpg_stmt_N__" does not exist`` (or a name collision with a
    *different* query already prepared under that generated name) under
    concurrent load. The org runs Postgres behind a transaction-mode
    PgBouncer, so the cache is disabled unconditionally rather than
    threaded as a knob — there is no deployment topology here where the
    default (ON) is safe.
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
