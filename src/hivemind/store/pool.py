"""Shared asyncpg pool construction for the Postgres adapters.

One small factory keeps the pgvector type-codec registration in a
single place: every connection in the pool registers the ``vector``
codec (``pgvector.asyncpg.register_vector``) so that vector columns
round-trip as plain Python lists (in) and ``pgvector.Vector`` (out).
"""

from __future__ import annotations

import asyncpg
from pgvector.asyncpg import register_vector


async def make_pool(dsn: str, min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
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

    return await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, init=init, statement_cache_size=0
    )
