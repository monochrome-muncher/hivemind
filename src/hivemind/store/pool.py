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
    """Build an asyncpg pool whose connections speak the pgvector codec."""

    async def init(conn: asyncpg.Connection) -> None:
        await register_vector(conn)

    return await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size, init=init)
