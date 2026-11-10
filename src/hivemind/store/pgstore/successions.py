"""Agent retirements, their undo and what is handed down."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from hivemind.domain.access import AgentStatus, InvalidAgentStatus, UnknownAgent
from hivemind.domain.succession import HandedDown, Succession, SuccessionRefused, UnknownSuccession
from hivemind.domain.validation import InvalidInput, is_valid_uuid
from hivemind.store.inheritance import INHERITED_FROM
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.rows import row_to_succession

LOCK_AGENT_ROWS = (
    "SELECT name, status FROM agents WHERE name = ANY($1::text[]) ORDER BY name FOR UPDATE"
)

_SUCCESSION_COLUMNS = 'retired_agent, successor, "by", at'

GET_SUCCESSION = f"SELECT {_SUCCESSION_COLUMNS} FROM successions WHERE retired_agent = $1"

LIST_SUCCESSIONS = f"SELECT {_SUCCESSION_COLUMNS} FROM successions ORDER BY at DESC, retired_agent"

INSERT_SUCCESSION = (
    'INSERT INTO successions (retired_agent, successor, "by") VALUES ($1, $2, $3) '
    f"RETURNING {_SUCCESSION_COLUMNS}"
)

DELETE_SUCCESSION = (
    f"DELETE FROM successions WHERE retired_agent = $1 RETURNING {_SUCCESSION_COLUMNS}"
)

REVOKE_ACTIVE_AGENT = "UPDATE agents SET status = 'revoked' WHERE name = $1 AND status = 'active'"

HANDED_DOWN = """
SELECT count(*) FILTER (WHERE e.scope = 'self') AS self_notes,
       count(*) FILTER (WHERE e.scope = 'fleet') AS fleet_memories,
       count(*) FILTER (WHERE e.scope = 'fleet'
                          AND e.fleet_id IS DISTINCT FROM $2::uuid) AS fleet_elsewhere
  FROM entries e WHERE e.state = 'active' AND e.author = ANY($1::text[])
"""

HANDED_DOWN_PINS = "SELECT count(*) FROM pins WHERE pinned_by = ANY($1::text[])"


class SuccessionStore(PgConnections):
    async def retire_agent(self, retired: str, successor: str, by: str) -> Succession:
        if retired == successor:
            raise InvalidInput("an agent cannot be its own successor")
        async with self._transaction() as conn:
            # Both rows in name order, so two opposite retirements cannot deadlock.
            status = {
                r["name"]: AgentStatus(r["status"])
                for r in await conn.fetch(LOCK_AGENT_ROWS, [retired, successor])
            }
            for name in (retired, successor):
                if name not in status:
                    raise UnknownAgent(f"unknown agent: {name}")
            if status[retired] is AgentStatus.PENDING:
                raise InvalidAgentStatus(retired, status[retired], "retire")
            if status[successor] is not AgentStatus.ACTIVE:
                raise InvalidAgentStatus(successor, status[successor], "make a successor of")
            standing = await conn.fetchrow(GET_SUCCESSION, retired)
            if standing is not None:
                raise SuccessionRefused(
                    f"agent {retired!r} is already retired in favour of {standing['successor']!r}"
                )
            if successor in await conn.fetchval(INHERITED_FROM, retired):
                raise SuccessionRefused(
                    f"{successor!r} already hands its memories down to {retired!r}, "
                    "directly or through others: that would be a cycle"
                )
            row = await conn.fetchrow(INSERT_SUCCESSION, retired, successor, by)
            await conn.execute(REVOKE_ACTIVE_AGENT, retired)
        if row is None:
            raise RuntimeError("succession row vanished between insert and read")
        return replace(row_to_succession(row), was=status[retired])

    async def undo_succession(self, retired: str) -> Succession:
        async with self._connection() as conn:
            row = await conn.fetchrow(DELETE_SUCCESSION, retired)
        if row is None:
            raise UnknownSuccession(f"no succession retires {retired!r}")
        return row_to_succession(row)

    async def get_succession(self, retired: str) -> Succession | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_SUCCESSION, retired)
        return row_to_succession(row) if row is not None else None

    async def list_successions(self) -> list[Succession]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_SUCCESSIONS)
        return [row_to_succession(r) for r in rows]

    async def inherited_authors(self, name: str) -> tuple[str, ...]:
        async with self._connection() as conn:
            return tuple(await conn.fetchval(INHERITED_FROM, name))

    async def handed_down(self, authors: Sequence[str], home_fleet_id: str | None) -> HandedDown:
        names = list(authors)
        home = home_fleet_id if home_fleet_id and is_valid_uuid(home_fleet_id) else None
        async with self._connection() as conn:
            counts = await conn.fetchrow(HANDED_DOWN, names, home)
            pins = await conn.fetchval(HANDED_DOWN_PINS, names)
        assert counts is not None  # an aggregate always returns a row
        return HandedDown(
            authors=tuple(names),
            self_notes=int(counts["self_notes"]),
            fleet_memories=int(counts["fleet_memories"]),
            fleet_memories_elsewhere=int(counts["fleet_elsewhere"]),
            pins=int(pins),
        )
