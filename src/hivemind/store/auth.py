"""The Postgres-backed ``Authenticator`` (ADR 0008 credential model).

Resolves a raw API key to a ``Credential`` via the ``credentials``
table: the key is stored only as a SHA-256 hash (the raw key is never
persisted), and the row's ``kind`` decides the credential's shape
(SPEC.md §8.1):

- ``admin``  → ``Credential(is_admin=True)``: may withdraw any entry.
- ``agent``  → the key binds an agent instance (``agent_id`` set), so
  attribution is server-verified, not self-reported.
- ``user``   → the key binds a user only; the agent self-reports its
  instance ID per request (SPEC.md §8.1), so ``agent_id`` is None.

Like ``PgStore``, the pool opens lazily on first use and closes via
``close()`` (SPEC.md §8.2, ADR 0007).
"""

from __future__ import annotations

import hashlib

import asyncpg

from hivemind.ports import Credential
from hivemind.store.pool import make_pool

_SELECT_CREDENTIAL = "SELECT kind, user_id, agent_id FROM credentials WHERE key_hash = $1"


def key_hash(key: str) -> str:
    """The SHA-256 hex digest of a raw API key (the stored identifier)."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class PgAuthenticator:
    """An ``Authenticator`` backed by the store lane's ``credentials`` table."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await make_pool(self._dsn)
        return self._pool

    async def close(self) -> None:
        """Tear down the pool (process-exit cleanup)."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def verify(self, key: str) -> Credential | None:
        """Resolve a key to its credential, or ``None`` if unknown."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SELECT_CREDENTIAL, key_hash(key))
        if row is None:
            return None
        kind = row["kind"]
        if kind == "admin":
            return Credential(user_id=row["user_id"], agent_id=row["agent_id"], is_admin=True)
        if kind == "agent":
            return Credential(user_id=row["user_id"], agent_id=row["agent_id"], is_admin=False)
        # kind == "user": the agent self-reports its instance ID (SPEC.md §8.1).
        return Credential(user_id=row["user_id"], agent_id=None, is_admin=False)
