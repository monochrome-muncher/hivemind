"""The Postgres-backed ``Authenticator`` (ADR 0008 credential model).

Resolves a raw API key to a ``Credential`` via the ``credentials``
table: the key is stored only as a SHA-256 hash (the raw key is never
persisted), and the row's ``kind`` decides the credential's shape
(SPEC.md §8.1):

- ``admin``  → ``Credential(is_admin=True)``: may withdraw any entry.
- ``agent``  → the key binds an agent instance (``agent_id`` set), so
  attribution is server-verified, not self-reported.
- ``user``   → retired (ADR 0012); since ADR 0039 a leftover row no
  longer verifies.

Like ``PgStore``, the pool opens lazily on first use and closes via
``close()`` (SPEC.md §8.2, ADR 0007).
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets

import asyncpg

from hivemind.domain.access import AgentStatus, InvalidAgentStatus, TrustLevel
from hivemind.domain.audit import key_fingerprint
from hivemind.ports import Credential
from hivemind.store.pool import make_pool

# One query resolves the key AND the agent it binds (ADR 0039): an agent key
# authenticates only while ``agents.status = 'active'``, so a key that
# outlives its agent's revocation (or never had an activation) is dead.
_SELECT_CREDENTIAL = (
    "SELECT c.kind, c.user_id, c.agent_id, c.agent_name, "
    "a.status AS agent_status, a.trust_level, a.home_fleet_id "
    "FROM credentials c LEFT JOIN agents a ON a.name = c.agent_name "
    "WHERE c.key_hash = $1"
)
_LOCK_AGENT = "SELECT status FROM agents WHERE name = $1 FOR UPDATE"
_DELETE_AGENT_KEYS = "DELETE FROM credentials WHERE kind = 'agent' AND agent_name = $1"
# Serialises org-key rotation (API + CLI share the key): the second rotation
# waits, then deletes the first's row. The unique index is the backstop.
ORG_KEY_LOCK = "SELECT pg_advisory_xact_lock(hashtext('hivemind:org-key'))"

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

    def __init__(self, dsn: str, *, pool_min_size: int = 1, pool_max_size: int = 10) -> None:
        self._dsn = dsn
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._pool: asyncpg.Pool | None = None
        # Serialises lazy creation: without it, every call arriving while the
        # first pool is still being built opened its own and orphaned it.
        self._pool_lock = asyncio.Lock()

    async def _ensure_pool(self) -> asyncpg.Pool:
        """Lazily open (and cache) the connection pool — exactly once, even
        when the first calls arrive concurrently."""
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    self._pool = await make_pool(
                        self._dsn, min_size=self._pool_min_size, max_size=self._pool_max_size
                    )
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
          * ``user``  → retired: ``None`` (ADR 0039).

        Every credential carries ``key_id`` — the key's non-secret
        fingerprint (first 12 hex chars of the stored hash), which is
        what the audit log records for admin actions (ADR 0027).
        """
        stored_hash = key_hash(key)
        key_id = key_fingerprint(stored_hash)
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(_SELECT_CREDENTIAL, stored_hash)
        if row is None:
            return None
        kind = row["kind"]
        if kind == "admin":
            return Credential(
                user_id=row["user_id"], agent_id=row["agent_id"], is_admin=True, key_id=key_id
            )
        if kind == "org":
            return Credential(
                user_id=row["user_id"],
                agent_id=row["agent_id"],
                is_org=True,
                access_controlled=True,
                key_id=key_id,
            )
        if kind == "agent":
            agent_name = row["agent_name"]
            if agent_name is None or row["agent_status"] != "active":
                # A name-less (pre-v2) key, a key with no agent record, or a
                # key whose agent is pending / revoked: not an identity.
                return None
            return Credential(
                user_id=agent_name,
                agent_id=row["agent_id"],
                agent_name=agent_name,
                access_controlled=True,
                trust_level=TrustLevel(row["trust_level"]),
                home_fleet_id=str(row["home_fleet_id"]) if row["home_fleet_id"] else None,
                key_id=key_id,
            )
        # kind == "user" (pre-v2, retired by ADR 0012): dead since ADR 0039.
        return None

    # -- key management (ADR 0012) ------------------------------------------

    async def issue_agent_key(self, agent_name: str) -> str:
        """Issue an agent key bound to the registered agent name; return
        the raw secret once (never stored).

        Runs under the agent row's lock and only for an ``active`` agent
        (``InvalidAgentStatus`` otherwise, ``KeyError`` if unknown), so a
        racing revoke can never leave a revoked agent holding a key
        (ADR 0039). Any stale key row (a revoke that flipped the status but
        did not finish deleting) is replaced: one live key per agent."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        async with pool.acquire() as conn, conn.transaction():
            status = await conn.fetchval(_LOCK_AGENT, agent_name)
            if status is None:
                raise KeyError(f"unknown agent: {agent_name}")
            if status != "active":
                raise InvalidAgentStatus(agent_name, AgentStatus(status), "issue a key for")
            await conn.execute(_DELETE_AGENT_KEYS, agent_name)
            await conn.execute(_ISSUE_AGENT_KEY, key_hash(raw_key), agent_name)
        return raw_key

    async def revoke_agent_key(self, agent_name: str) -> None:
        """Retire the agent's key (the agent record + name stay reserved,
        ADR 0012: demotion != revocation, but a revoked key is dead).
        Call it after the agent's status has flipped to ``revoked``."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            # Same row lock as issuance: the two are serialised (ADR 0039).
            # Delete only while the agent is still revoked: if a racing
            # activation re-activated it after the caller's status flip,
            # the order was revoke-then-activate and the fresh key is valid.
            status = await conn.fetchval(_LOCK_AGENT, agent_name)
            if status is None or status == "revoked":
                await conn.execute(_REVOKE_AGENT_KEY, agent_name)

    async def rotate_org_key(self) -> str:
        """Rotate the shared org key (closes registration, ADR 0031);
        returns the new raw secret once. All prior org keys stop working."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(ORG_KEY_LOCK)
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
