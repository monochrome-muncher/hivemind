"""The Postgres-backed ``Authenticator`` (ADR 0008 credential model).

Keys are stored only as SHA-256 hashes (SPEC.md §8.1); see ``verify``
for how a row's ``kind`` maps to a ``Credential``. Like ``PgStore``, the
pool opens lazily (ADR 0007).
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets

import asyncpg

from hivemind.domain.access import AgentStatus, InvalidAgentStatus, TrustLevel
from hivemind.domain.audit import key_fingerprint
from hivemind.ports import Credential
from hivemind.store.pool import PoolTimeouts, make_pool

# Key and agent in one query: an agent key authenticates only while its
# agent is active (ADR 0039).
_SELECT_CREDENTIAL = (
    "SELECT c.kind, c.user_id, c.agent_id, c.agent_name, "
    "a.status AS agent_status, a.trust_level, a.home_fleet_id "
    "FROM credentials c LEFT JOIN agents a ON a.name = c.agent_name "
    "WHERE c.key_hash = $1"
)
# Rows ``verify`` can never resolve (ADR 0039). Inert, but they would live
# again on a rollback to a release that does not join ``agents``.
DEAD_CREDENTIAL_PREDICATE = (
    "(c.kind = 'user' OR (c.kind = 'agent' AND (c.agent_name IS NULL OR NOT EXISTS "
    "(SELECT 1 FROM agents a WHERE a.name = c.agent_name AND a.status = 'active'))))"
)
COUNT_DEAD_CREDENTIALS = f"SELECT count(*) FROM credentials c WHERE {DEAD_CREDENTIAL_PREDICATE}"
_LOCK_AGENT = "SELECT status FROM agents WHERE name = $1 FOR UPDATE"
_DELETE_AGENT_KEYS = "DELETE FROM credentials WHERE kind = 'agent' AND agent_name = $1"
# Serialises org-key rotation (API + CLI share the key): the second rotation
# waits, then deletes the first's row. The unique index is the backstop.
ORG_KEY_LOCK = "SELECT pg_advisory_xact_lock(hashtext('hivemind:org-key'))"

# Key management (ADR 0012).
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

    def __init__(
        self,
        dsn: str,
        *,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        timeouts: PoolTimeouts | None = None,
    ) -> None:
        self._dsn = dsn
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._timeouts = timeouts or PoolTimeouts()
        self._acquire_timeout = self._timeouts.acquire
        self._pool: asyncpg.Pool | None = None
        # Serialises lazy creation so concurrent first calls share one pool.
        self._pool_lock = asyncio.Lock()

    async def _ensure_pool(self) -> asyncpg.Pool:
        """Lazily open (and cache) the connection pool — exactly once, even
        when the first calls arrive concurrently."""
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    self._pool = await make_pool(
                        self._dsn,
                        min_size=self._pool_min_size,
                        max_size=self._pool_max_size,
                        command_timeout=self._timeouts.command,
                        statement_timeout_ms=self._timeouts.statement_ms,
                        connect_timeout=self._timeouts.connect,
                    )
        return self._pool

    async def close(self) -> None:
        """Tear down the pool (process-exit cleanup)."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def verify(self, key: str) -> Credential | None:
        """Resolve a key to its credential, or ``None`` if unknown.

        By ``kind`` (ADR 0012): ``admin`` → full access; ``org`` →
        registration + health only; ``agent`` → name, trust level and home
        fleet from ``agents`` (ADR 0011), only while active; ``user`` →
        retired, ``None`` (ADR 0039). ``key_id`` is the key fingerprint
        for the audit log (ADR 0027).
        """
        stored_hash = key_hash(key)
        key_id = key_fingerprint(stored_hash)
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
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
        """Issue an agent key; return the raw secret once.

        Under the agent row's lock and only while ``active``, so a racing
        revoke cannot leave a revoked agent with a key (ADR 0039). Any
        stale key row is replaced: one live key per agent."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            status = await conn.fetchval(_LOCK_AGENT, agent_name)
            if status is None:
                raise KeyError(f"unknown agent: {agent_name}")
            if status != "active":
                raise InvalidAgentStatus(agent_name, AgentStatus(status), "issue a key for")
            await conn.execute(_DELETE_AGENT_KEYS, agent_name)
            await conn.execute(_ISSUE_AGENT_KEY, key_hash(raw_key), agent_name)
        return raw_key

    async def revoke_agent_key(self, agent_name: str) -> None:
        """Retire the agent's key; call after the status flipped to
        ``revoked`` (the name stays reserved, ADR 0012)."""
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            # Same row lock as issuance (ADR 0039). If a racing activation
            # re-activated the agent, its fresh key is valid: keep it.
            status = await conn.fetchval(_LOCK_AGENT, agent_name)
            if status is None or status == "revoked":
                await conn.execute(_REVOKE_AGENT_KEY, agent_name)

    async def rotate_org_key(self) -> str:
        """Rotate the shared org key (closes registration, ADR 0031);
        returns the new raw secret once. All prior org keys stop working."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            await conn.execute(ORG_KEY_LOCK)
            await conn.execute(_DELETE_ORG_KEY)
            await conn.execute(_ISSUE_ORG_KEY, key_hash(raw_key), "org")
        return raw_key

    async def issue_admin_key(self) -> str:
        """Issue an admin key; return the raw secret once."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            await conn.execute(_ISSUE_ADMIN_KEY, key_hash(raw_key), "admin")
        return raw_key
