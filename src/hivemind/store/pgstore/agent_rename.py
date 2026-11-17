"""Renaming an agent (ADR 0159): every live copy of its name, rewritten in one transaction.

The name is the identity and is stored as plain text, without foreign keys, in the tables below.
The audit log is append-only and is never rewritten (ADR 0027); the name an agent leaves is kept
in ``agent_former_names`` so no other agent can take it.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from functools import partial

import asyncpg
from asyncpg import Record

from hivemind.domain.access import Agent, AgentNameTaken, FleetBusy, UnknownAgent
from hivemind.store.pgstore.agent_cleanup import CLEANUP_AGENT_LOCK
from hivemind.store.pgstore.agents import AGENT_NAME_LOCK, GET_AGENT
from hivemind.store.pgstore.connection import (
    LONG_OP_TIMEOUT,
    MERGE_SETTINGS,
    PgConnections,
    refuse_on_contention,
)
from hivemind.store.pgstore.rows import row_to_agent

CURRENT_NAME_HOLDER = "SELECT name FROM agents WHERE lower(name) = lower($1) AND name <> $2"

# The only lock on an agent row that conflicts with a write's key share (ADR 0159): the name is the
# key. Every other locker uses NO KEY UPDATE.
LOCK_AGENT_FOR_RENAME = "SELECT * FROM agents WHERE name = $1 FOR UPDATE"

FORMER_NAME_RESERVED_BY = "SELECT agent FROM agent_former_names WHERE lower(name) = lower($1)"

FORMER_NAMES_OF = (
    "SELECT agent, name FROM agent_former_names WHERE agent = ANY($1::text[]) "
    "ORDER BY renamed_at, name"
)

FOLLOW_FORMER_NAMES = "UPDATE agent_former_names SET agent = $2 WHERE agent = $1"

# The new name is current again when it was one of the agent's own former names.
FORGET_FORMER_NAME = "DELETE FROM agent_former_names WHERE lower(name) = lower($1)"

# A case-only change leaves nothing to reserve, and two legacy case variants (ADR 0045) may leave
# the same folded name: the first reservation stands.
KEEP_FORMER_NAME = (
    "INSERT INTO agent_former_names (name, agent) VALUES ($1, $2) "
    "ON CONFLICT ((lower(name))) DO NOTHING"
)


def _rewrite_pinned_by(table: str, column: str) -> str:
    """Rewrite ``pinned_by`` inside the pins an undoable merge or cleanup recorded."""
    return f"""
UPDATE {table} SET {column} = (
    SELECT jsonb_agg(
               CASE WHEN p.pin ->> 'pinned_by' = $1
                    THEN jsonb_set(p.pin, '{{pinned_by}}', to_jsonb($2::text))
                    ELSE p.pin END
               ORDER BY p.position)
      FROM jsonb_array_elements({column}) WITH ORDINALITY AS p(pin, position))
 WHERE undone_at IS NULL
   AND {column} @> jsonb_build_array(jsonb_build_object('pinned_by', $1::text))
"""


# ``$1`` is the old name, ``$2`` the new one. An ``agent`` column that holds the name is the
# agent's own identity when the key wrote it (``credential.agent_id``), so it follows; one that
# holds anything else is a label the caller reported about itself and stays.
REWRITES = (
    "UPDATE agents SET name = $2 WHERE name = $1",
    "UPDATE credentials SET user_id = $2, agent_id = $2, agent_name = $2 "
    "WHERE kind = 'agent' AND agent_name = $1",
    "UPDATE entries SET author = $2, agent = CASE WHEN agent = $1 THEN $2 ELSE agent END "
    "WHERE author = $1",
    'UPDATE feedbacks SET "user" = $2, agent = CASE WHEN agent = $1 THEN $2 ELSE agent END '
    'WHERE "user" = $1',
    "UPDATE pins SET pinned_by = $2 WHERE pinned_by = $1",
    "UPDATE successions SET retired_agent = $2 WHERE retired_agent = $1",
    "UPDATE successions SET successor = $2 WHERE successor = $1",
    "UPDATE agent_cleanups SET agent = $2 WHERE agent = $1",
    "UPDATE agent_cleanups SET cleaned_by = $2 WHERE cleaned_by = $1",
    _rewrite_pinned_by("agent_cleanups", "unpinned_pins"),
    "UPDATE fleet_merges SET moved_agents = array_replace(moved_agents, $1, $2) "
    "WHERE $1 = ANY(moved_agents)",
    _rewrite_pinned_by("fleet_merges", "dropped_pins"),
)


async def _refuse_taken(conn: asyncpg.Connection, old: str, new: str) -> None:
    if await conn.fetchval(CURRENT_NAME_HOLDER, new, old) is not None:
        raise AgentNameTaken(new)
    holder = await conn.fetchval(FORMER_NAME_RESERVED_BY, new)
    if holder is not None and holder != old:
        raise AgentNameTaken(new)


def _rename_contention() -> AbstractContextManager[None]:
    """A lock timeout or a deadlock (with a fleet merge or move, which lock entries before agents) is
    the retryable ``FleetBusy``, as for a move."""
    return refuse_on_contention(
        partial(FleetBusy, ""),
        "the rename waited too long for a lock held by a write or a fleet change; retry in a few "
        "seconds",
        "the rename deadlocked with a concurrent change; retry in a few seconds",
    )


async def rename_agent_rows(conn: asyncpg.Connection, old: str, new: str) -> tuple[Record, Record]:
    """Rename ``old`` to ``new`` on ``conn``'s open transaction; returns the agent row before and
    after. Locks the new name (as registration does), the old name's cleanups and the agent row.
    ``UnknownAgent``; ``AgentNameTaken``; ``FleetBusy`` on lock contention. The same name changes
    nothing. It runs past the pool's command timeout: it rewrites every memory the agent wrote."""
    with _rename_contention():
        await conn.execute(MERGE_SETTINGS)
        await conn.execute(AGENT_NAME_LOCK, new)
        await conn.execute(CLEANUP_AGENT_LOCK, old)
        before = await conn.fetchrow(LOCK_AGENT_FOR_RENAME, old)
        if before is None:
            raise UnknownAgent(f"unknown agent: {old}")
        if old == new:
            return before, before
        await _refuse_taken(conn, old, new)
        await conn.execute(FOLLOW_FORMER_NAMES, old, new)
        await conn.execute(FORGET_FORMER_NAME, new)
        if old.lower() != new.lower():
            await conn.execute(KEEP_FORMER_NAME, old, new)
        for statement in REWRITES:
            await conn.execute(statement, old, new, timeout=LONG_OP_TIMEOUT)
        after = await conn.fetchrow(GET_AGENT, new)
    if after is None:
        raise RuntimeError(f"agent {old!r} vanished during its rename")
    return before, after


class AgentRenameStore(PgConnections):
    async def rename_agent(self, name: str, new_name: str) -> tuple[Agent, Agent]:
        """Rename an agent, any status (ADR 0159); returns (before, after)."""
        async with self._transaction() as conn:
            before, after = await rename_agent_rows(conn, name, new_name)
        return row_to_agent(before), row_to_agent(after)

    async def former_agent_names(self, names: Sequence[str]) -> dict[str, tuple[str, ...]]:
        async with self._connection() as conn:
            rows = await conn.fetch(FORMER_NAMES_OF, list(names))
        left: dict[str, tuple[str, ...]] = dict.fromkeys(names, ())
        for row in rows:
            left[row["agent"]] = (*left[row["agent"]], row["name"])
        return left
