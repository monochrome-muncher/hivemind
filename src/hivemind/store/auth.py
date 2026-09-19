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
import secrets

import asyncpg

from hivemind.domain.access import TrustLevel
from hivemind.ports import Credential
from hivemind.store.pool import make_pool

_SELECT_CREDENTIAL = (
    "SELECT kind, user_id, agent_id, agent_name FROM credentials WHERE key_hash = $1"
)
_SELECT_AGENT = "SELECT trust_level, home_fleet_id FROM agents WHERE name = $1"

# Key-management (ADR 0012): raw keys are random 32-byte secrets printed
# ONCE at issuance; only their SHA-256 hash is stored (a leaked database
# never leaks usable keys — SPEC.md §8.1).
_ISSUE_AGENT_KEY = (
    "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
    "VALUES ($1, 'agent', $2, $2, $2)"
)
_REVOKE_AGENT_KEY = "DELETE FROM credentials WHERE kind = 'agent' AND agent_name = $1"
_ISSUE_ADMIN_KEY = "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'admin', $2)"
_ISSUE_ORG_KEY = "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'org', $2)"
_DELETE_ORG_KEY = "DELETE FROM credentials WHERE kind = 'org'"


def _generate_key() -> str:
    """A fresh random API key (32 bytes, URL-safe). The raw secret is
    returned to the caller exactly once; only its hash is persisted."""
    return "hm_" + secrets.token_urlsafe(32)


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
        """Resolve a key to its credential, or ``None`` if unknown.

        The row's ``kind`` decides the credential's shape (ADR 0012):
          * ``admin`` → admin credential (full access).
          * ``org``   → the shared org key (gates registration + health
            only; all data-plane privilege comes from the agent key).
          * ``agent`` → an agent credential: the agent's registered name
            (``agent_name``) becomes the entry's ``author``; its trust
            level + home fleet are resolved from the ``agents`` table
            (ADRs 0011-0012).
          * ``user``  → legacy v1 (the agent self-reports its instance ID,
            SPEC.md §8.1).
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SELECT_CREDENTIAL, key_hash(key))
        if row is None:
            return None
        kind = row["kind"]
        if kind == "admin":
            return Credential(user_id=row["user_id"], agent_id=row["agent_id"], is_admin=True)
        if kind == "org":
            return Credential(
                user_id=row["user_id"],
                agent_id=row["agent_id"],
                is_org=True,
                access_controlled=True,
            )
        if kind == "agent":
            agent_name = row["agent_name"]
            if agent_name is None:
                # Legacy agent key (no registered name): v1 behavior (the
                # agent self-reports its instance ID, SPEC.md §8.1).
                return Credential(user_id=row["user_id"], agent_id=row["agent_id"])
            # Resolve the agent's privilege (trust level + home fleet) from
            # the ``agents`` table (ADRs 0011-0012).
            pool = await self._ensure_pool()
            async with pool.acquire() as conn:
                agent_row = await conn.fetchrow(_SELECT_AGENT, agent_name)
            if agent_row is None:
                # No agents record (not activated): untrusted (level 0).
                return Credential(
                    user_id=agent_name,
                    agent_id=row["agent_id"],
                    agent_name=agent_name,
                    access_controlled=True,
                    trust_level=TrustLevel.UNTRUSTED,
                )
            return Credential(
                user_id=agent_name,
                agent_id=row["agent_id"],
                agent_name=agent_name,
                access_controlled=True,
                trust_level=TrustLevel(agent_row["trust_level"]),
                home_fleet_id=(
                    str(agent_row["home_fleet_id"]) if agent_row["home_fleet_id"] else None
                ),
            )
        # kind == "user" (legacy v1): the agent self-reports its instance ID.
        return Credential(user_id=row["user_id"], agent_id=row["agent_id"])

    # -- key management (ADR 0012) ------------------------------------------

    async def issue_agent_key(self, agent_name: str) -> str:
        """Issue an agent key bound to the registered agent name; return
        the raw secret once (never stored)."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        async with pool.acquire() as conn:
            await conn.execute(_ISSUE_AGENT_KEY, key_hash(raw_key), agent_name)
        return raw_key

    async def revoke_agent_key(self, agent_name: str) -> None:
        """Retire the agent's key (the agent record + name stay reserved,
        ADR 0012: demotion != revocation, but a revoked key is dead)."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(_REVOKE_AGENT_KEY, agent_name)

    async def rotate_org_key(self) -> str:
        """Rotate the shared org key (the cluster kill switch, ADR 0012);
        returns the new raw secret once. All prior org keys stop working."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(_DELETE_ORG_KEY)
            await conn.execute(_ISSUE_ORG_KEY, key_hash(raw_key), "org")
        return raw_key

    async def issue_admin_key(self) -> str:
        """Issue an admin key; return the raw secret once."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        async with pool.acquire() as conn:
            await conn.execute(_ISSUE_ADMIN_KEY, key_hash(raw_key), "admin")
        return raw_key
