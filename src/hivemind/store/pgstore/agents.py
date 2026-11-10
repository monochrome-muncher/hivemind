"""Agent registration, activation, revocation and settings."""

from __future__ import annotations

from datetime import datetime

import asyncpg

from hivemind.domain.access import Agent, AgentStatus, InvalidAgentStatus, TrustLevel, UnknownAgent
from hivemind.domain.succession import AgentRetired
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.fleet_guards import refuse_merged
from hivemind.store.pgstore.rows import row_to_agent
from hivemind.store.pgstore.successions import GET_SUCCESSION, LOCK_AGENT_ROWS

GET_AGENT = "SELECT * FROM agents WHERE name = $1"

GET_AGENT_FOR_UPDATE = "SELECT * FROM agents WHERE name = $1 FOR UPDATE"

# Agent names are ASCII (ADR 0040), so lower() is their case fold. The oldest
# holder wins when legacy case variants already coexist (ADR 0045).
GET_AGENT_FOLDED = "SELECT * FROM agents WHERE lower(name) = lower($1) ORDER BY created_at LIMIT 1"

# Serialises registrations of one case-folded name (`Bob` and `bob` cannot both pass the check).
AGENT_NAME_LOCK = "SELECT pg_advisory_xact_lock(hashtext('hivemind:agent-name:' || lower($1)))"

INSERT_AGENT = (
    "INSERT INTO agents (name, owner_alias, status, trust_level) "
    "VALUES ($1, $2, 'pending', 0) ON CONFLICT (name) DO NOTHING RETURNING *"
)

# Key use (ADR 0110) rides along; one key per agent (ADR 0039), so the join adds no rows.
LIST_AGENTS = (
    "SELECT a.*, c.last_used_at, c.created_at AS key_created_at, "
    "c.locked_at AS key_locked_at FROM agents a "
    "LEFT JOIN credentials c ON c.kind = 'agent' AND c.agent_name = a.name "
    "ORDER BY a.name"
)

AGENT_KEY_LAST_USED = (
    "SELECT last_used_at FROM credentials WHERE kind = 'agent' AND agent_name = $1"
)

# ADR 0028: the status guard is in the WHERE, so check-and-flip is one atomic statement.
ACTIVATE_AGENT = (
    "UPDATE agents SET status = 'active', trust_level = $2, home_fleet_id = $3, "
    "activated_at = now() WHERE name = $1 AND status IN ('pending', 'revoked') RETURNING *"
)

REVOKE_AGENT = (
    "UPDATE agents SET status = 'revoked' "
    "WHERE name = $1 AND status IN ('pending', 'active') RETURNING *"
)

SET_AGENT_LEVEL = "UPDATE agents SET trust_level = $2 WHERE name = $1 RETURNING *"

SET_AGENT_OWNER = "UPDATE agents SET owner_alias = $2 WHERE name = $1"

SET_AGENT_FLEET = "UPDATE agents SET home_fleet_id = $2 WHERE name = $1 RETURNING *"


class AgentStore(PgConnections):
    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register an agent idempotently (SPEC §12.3): an existing record, a racing duplicate
        or a case-only variant comes back unchanged (ADRs 0039, 0045); a new one is ``pending``."""
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_AGENT, name)
            if row is None:
                async with conn.transaction():
                    await conn.execute(AGENT_NAME_LOCK, name)
                    row = await conn.fetchrow(GET_AGENT_FOLDED, name)
                    if row is None:
                        row = await conn.fetchrow(INSERT_AGENT, name, owner_alias)
                    if row is None:
                        # A concurrent registration won: return its record.
                        row = await conn.fetchrow(GET_AGENT, name)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between insert and read")
        return row_to_agent(row)

    async def get_agent(self, name: str) -> Agent | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_AGENT, name)
        return row_to_agent(row) if row is not None else None

    async def list_agents(self) -> list[Agent]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_AGENTS)
        return [row_to_agent(r) for r in rows]

    async def agent_key_last_used(self, name: str) -> datetime | None:
        async with self._connection() as conn:
            last_used: datetime | None = await conn.fetchval(AGENT_KEY_LAST_USED, name)
        return last_used

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Activate a pending or revoked agent (ADRs 0012, 0028); ``InvalidAgentStatus`` if
        active."""
        async with self._write_transaction(home_fleet_id) as conn:
            await refuse_merged(conn, home_fleet_id)
            # Under the row lock a retirement holds, so a retired agent cannot reactivate (ADR
            # 0113).
            await conn.fetchrow(LOCK_AGENT_ROWS, [name])
            retired = await conn.fetchrow(GET_SUCCESSION, name)
            if retired is not None:
                raise AgentRetired(name, retired["successor"])
            row = await conn.fetchrow(ACTIVATE_AGENT, name, trust_level.value, home_fleet_id)
            if row is None:
                await self._raise_for_status(conn, name, "activate")
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between flip and read")
        return row_to_agent(row)

    async def revoke_agent(self, name: str) -> Agent:
        """Flip a pending or active agent to ``revoked``; ``InvalidAgentStatus`` if already
        revoked."""
        async with self._connection() as conn:
            row = await conn.fetchrow(REVOKE_AGENT, name)
            if row is None:
                await self._raise_for_status(conn, name, "revoke")
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between flip and read")
        return row_to_agent(row)

    @staticmethod
    async def _raise_for_status(conn: asyncpg.Connection, name: str, verb: str) -> None:
        """A guarded lifecycle UPDATE matched nothing: say why."""
        existing = await conn.fetchrow(GET_AGENT, name)
        if existing is None:
            raise UnknownAgent(f"unknown agent: {name}")
        raise InvalidAgentStatus(name, AgentStatus(existing["status"]), verb)

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Level 0 is *dormant* (active, no access), distinct from revocation (ADR 0012)."""
        async with self._connection() as conn:
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                raise UnknownAgent(f"unknown agent: {name}")
            row = await conn.fetchrow(SET_AGENT_LEVEL, name, level.value)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between check and update")
        return row_to_agent(row)

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent; its earlier ``fleet``-scoped entries stay where they were written."""
        async with self._write_transaction(fleet_id) as conn:
            await refuse_merged(conn, fleet_id)
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                raise UnknownAgent(f"unknown agent: {name}")
            row = await conn.fetchrow(SET_AGENT_FLEET, name, fleet_id)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between check and update")
        return row_to_agent(row)

    async def set_agent_owner_alias(self, name: str, owner_alias: str) -> tuple[Agent, Agent]:
        """Set an agent's owner alias (ADR 0122); returns (before, after)."""
        async with self._transaction() as conn:
            existing = await conn.fetchrow(GET_AGENT_FOR_UPDATE, name)
            if existing is None:
                raise UnknownAgent(f"unknown agent: {name}")
            await conn.execute(SET_AGENT_OWNER, name, owner_alias)
            row = await conn.fetchrow(GET_AGENT, name)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between check and update")
        return row_to_agent(existing), row_to_agent(row)
