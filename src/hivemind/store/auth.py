"""The Postgres-backed ``Authenticator`` (ADRs 0012, 0039). Keys are stored only as SHA-256
hashes (SPEC §8.1); the pool opens lazily, like ``PgStore``'s."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time

import asyncpg

from hivemind.domain.access import (
    AgentStatus,
    InvalidAgentStatus,
    KeyLocked,
    StoreUnavailable,
    TrustLevel,
    UnknownAgent,
)
from hivemind.domain.audit import ActorKind, AuditAction, key_fingerprint
from hivemind.ports import Credential, KeyReplacement
from hivemind.store.inheritance import INHERITED_FROM_AGENT_ROW
from hivemind.store.pool import PoolTimeouts, make_pool

logger = logging.getLogger(__name__)

# The stamp is best effort: a row lock held elsewhere must not stall a request for the command
# timeout.
_STAMP_TIMEOUT_S = 1.0
# A broken stamp fails every stale key check; log it once a minute per process.
_WARN_EVERY_S = 60.0
_last_warned: float | None = None

# Key, agent and its heirs in one query: an agent key authenticates only while its agent is
# active (ADR 0039); the succession walk (ADR 0113) adds no round trip.
_SELECT_CREDENTIAL = (
    "SELECT c.kind, c.user_id, c.agent_id, c.agent_name, "
    "a.status AS agent_status, a.trust_level, a.home_fleet_id, "
    "(c.locked_at IS NOT NULL) AS key_locked, "
    "(c.last_used_at IS NULL OR c.last_used_at < now() - interval '1 hour') AS use_stale, "
    f"{INHERITED_FROM_AGENT_ROW} AS inherits_from "
    "FROM credentials c LEFT JOIN agents a ON a.name = c.agent_name "
    "WHERE c.key_hash = $1"
)
# ADR 0110. The WHERE repeats the staleness test, so racing requests write once.
_TOUCH_CREDENTIAL = (
    "UPDATE credentials SET last_used_at = now() WHERE key_hash = $1 "
    "AND (last_used_at IS NULL OR last_used_at < now() - interval '1 hour')"
)
# By agent and fingerprint, only while unlocked: a second press or a replaced key does nothing.
_LOCK_AGENT_KEY = (
    "UPDATE credentials SET locked_at = now() WHERE kind = 'agent' AND agent_name = $1 "
    "AND starts_with(key_hash, $2) AND locked_at IS NULL RETURNING key_hash"
)
# Rows ``verify`` can never resolve (ADR 0039): inert, but live again on a rollback to a release
# that does not join ``agents``.
DEAD_CREDENTIAL_PREDICATE = (
    "(c.kind = 'user' OR (c.kind = 'agent' AND (c.agent_name IS NULL OR NOT EXISTS "
    "(SELECT 1 FROM agents a WHERE a.name = c.agent_name AND a.status = 'active'))))"
)
COUNT_DEAD_CREDENTIALS = f"SELECT count(*) FROM credentials c WHERE {DEAD_CREDENTIAL_PREDICATE}"
_LOCK_AGENT = "SELECT status FROM agents WHERE name = $1 FOR UPDATE"
_DELETE_AGENT_KEYS = "DELETE FROM credentials WHERE kind = 'agent' AND agent_name = $1"
_INSERT_AUDIT = (
    "INSERT INTO audit_log (actor_kind, actor, action, target, detail) "
    "VALUES ($1, $2, $3, $4, $5::jsonb)"
)
_DELETE_AGENT_KEYS_RETURNING = _DELETE_AGENT_KEYS + " RETURNING key_hash"
# Serialises org-key rotation (API and CLI): the second waits, then deletes the first's row.
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
    """A fresh API key, returned to the caller once; only its hash is persisted."""
    return "hm_" + secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    """The SHA-256 hex digest of a raw API key: the stored identifier."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


async def _record_use(conn: asyncpg.Connection, stored_hash: str, key_id: str) -> None:
    """Stamp the key's last use, at most hourly (ADR 0110), on ``verify``'s connection. Never fails
    the request: database, connection and timeout errors are logged by fingerprint and swallowed.
    Only those are caught, so a bug in the statement still surfaces in tests."""
    global _last_warned
    try:
        await conn.execute(_TOUCH_CREDENTIAL, stored_hash, timeout=_STAMP_TIMEOUT_S)
    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError) as exc:
        now = time.monotonic()
        if _last_warned is None or now - _last_warned >= _WARN_EVERY_S:
            _last_warned = now
            logger.warning("could not record last use of key %s: %s", key_id, type(exc).__name__)


# Transient connection-level failures only: sockets, SQLSTATE class 08, 57P01-57P03, 53300.
_TRANSIENT_STORE_ERRORS = (
    OSError,
    asyncpg.InterfaceError,
    asyncpg.PostgresConnectionError,
    asyncpg.CannotConnectNowError,
    asyncpg.AdminShutdownError,
    asyncpg.CrashShutdownError,
    asyncpg.TooManyConnectionsError,
)


def _credential_from_row(row: asyncpg.Record, key_id: str) -> Credential | None:
    """The ``Credential`` a key row stands for by ``kind`` (ADR 0012), or ``None`` if not an
    identity (ADR 0039)."""
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
        if agent_name is None or row["agent_status"] != AgentStatus.ACTIVE:
            # Name-less, agent-less, pending or revoked: not an identity.
            return None
        if row["key_locked"]:
            raise KeyLocked
        return Credential(
            user_id=agent_name,
            agent_id=row["agent_id"],
            agent_name=agent_name,
            access_controlled=True,
            trust_level=TrustLevel(row["trust_level"]),
            home_fleet_id=str(row["home_fleet_id"]) if row["home_fleet_id"] else None,
            key_id=key_id,
            inherits_from=tuple(row["inherits_from"] or ()),
        )
    # kind == "user" (pre-v2, retired by ADR 0012): dead since ADR 0039.
    return None


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
        # Concurrent first calls share one pool.
        self._pool_lock = asyncio.Lock()

    async def _ensure_pool(self) -> asyncpg.Pool:
        """Open the pool once, even for concurrent first calls."""
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
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def verify(self, key: str) -> Credential | None:
        """Resolve a key to its credential by ``kind`` (ADR 0012), or ``None`` if unknown. ``agent``
        keys resolve only while the agent is active and raise ``KeyLocked`` if locked (ADR 0117);
        ``user`` keys are retired (ADR 0039). ``key_id`` is the fingerprint (ADR 0027)."""
        stored_hash = key_hash(key)
        key_id = key_fingerprint(stored_hash)
        try:
            pool = await self._ensure_pool()
            async with pool.acquire(timeout=self._acquire_timeout) as conn:
                row = await conn.fetchrow(_SELECT_CREDENTIAL, stored_hash)
                if row is None:
                    return None
                credential = _credential_from_row(row, key_id)
                if credential is not None and row["use_stale"]:
                    await _record_use(conn, stored_hash, key_id)
        except _TRANSIENT_STORE_ERRORS as exc:
            # A connection failure says nothing about the key: retryable, never a 500 or 401. Keep
            # only the type, never its text. Permanent faults are not in the tuple and stay loud.
            raise StoreUnavailable(type(exc).__name__) from None
        return credential

    async def issue_agent_key(self, agent_name: str) -> str:
        """Issue an agent key, returning the raw secret once. Under the agent row's lock and only
        while ``active``, so a racing revoke leaves no key (ADR 0039); one live key per agent."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            status = await conn.fetchval(_LOCK_AGENT, agent_name)
            if status is None:
                raise UnknownAgent(f"unknown agent: {agent_name}")
            if status != AgentStatus.ACTIVE:
                raise InvalidAgentStatus(agent_name, AgentStatus(status), "issue a key for")
            await conn.execute(_DELETE_AGENT_KEYS, agent_name)
            await conn.execute(_ISSUE_AGENT_KEY, key_hash(raw_key), agent_name)
        return raw_key

    async def replace_agent_key(
        self, agent_name: str, *, audit_actor_kind: ActorKind, audit_actor: str
    ) -> KeyReplacement:
        """Swap an active agent's key (ADR 0108). Delete, insert and audit row share one transaction
        under the agent row's lock: never zero or two keys, and a racing revoke leaves none (ADR
        0039)."""
        pool = await self._ensure_pool()
        raw_key = _generate_key()
        new_hash = key_hash(raw_key)
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            status = await conn.fetchval(_LOCK_AGENT, agent_name)
            if status is None:
                raise UnknownAgent(f"unknown agent: {agent_name}")
            if status != AgentStatus.ACTIVE:
                raise InvalidAgentStatus(agent_name, AgentStatus(status), "replace the key of")
            old = await conn.fetch(_DELETE_AGENT_KEYS_RETURNING, agent_name)
            await conn.execute(_ISSUE_AGENT_KEY, new_hash, agent_name)
            replacement = KeyReplacement(
                raw_key,
                key_fingerprint(new_hash),
                key_fingerprint(old[0]["key_hash"]) if old else None,
            )
            await conn.execute(
                _INSERT_AUDIT,
                audit_actor_kind.value,
                audit_actor,
                AuditAction.AGENT_KEY_REPLACE.value,
                agent_name,
                json.dumps({"old_key": replacement.old_key_id, "new_key": replacement.new_key_id}),
            )
        return replacement

    async def lock_agent_key(self, agent_name: str, *, key_id: str) -> bool:
        """Lock the agent's own key (ADR 0117); the lock and its audit row share a transaction."""
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            locked = await conn.fetchrow(_LOCK_AGENT_KEY, agent_name, key_id)
            if locked is None:
                return False
            await conn.execute(
                _INSERT_AUDIT,
                ActorKind.AGENT_KEY.value,
                agent_name,
                AuditAction.AGENT_KEY_LOCK.value,
                agent_name,
                json.dumps({"key": key_fingerprint(locked["key_hash"]), "via": "portal"}),
            )
        return True

    async def revoke_agent_key(self, agent_name: str) -> None:
        """Retire the agent's key; call after the status flipped to ``revoked`` (ADR 0012)."""
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            # Same row lock as issuance (ADR 0039): a racing re-activation's fresh key stays.
            status = await conn.fetchval(_LOCK_AGENT, agent_name)
            if status is None or status == "revoked":
                await conn.execute(_REVOKE_AGENT_KEY, agent_name)

    async def rotate_org_key(self) -> str:
        """Rotate the org key (ADR 0031), returning the new secret once; prior org keys stop
        working."""
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
