"""Fleets: create, list, get, rename and set the phase."""

from __future__ import annotations

import asyncpg

from hivemind.domain.access import Fleet, FleetExists, FleetMerged, UnknownFleet
from hivemind.domain.fleet_merge import FleetFacts
from hivemind.domain.fleet_phase import FleetPhase
from hivemind.domain.validation import is_valid_uuid
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.rows import row_to_fleet

FLEET_AGENT_COUNT = "SELECT count(*) FROM agents WHERE home_fleet_id = $1"

FLEET_ENTRY_STATES = "SELECT state, count(*) AS n FROM entries WHERE fleet_id = $1 GROUP BY state"

SELECT_FLEET_BY_NAME = "SELECT 1 FROM fleets WHERE name = $1"

_FLEET_COLUMNS = "id, name, created_at, merged_into, merged_at, phase, phase_until"

INSERT_FLEET = (
    "INSERT INTO fleets (name) VALUES ($1) "
    f"ON CONFLICT (name) DO NOTHING RETURNING {_FLEET_COLUMNS}"
)

RENAME_FLEET = f"UPDATE fleets SET name = $2 WHERE id = $1 RETURNING {_FLEET_COLUMNS}"

SET_FLEET_PHASE = (
    f"UPDATE fleets SET phase = $2, phase_until = $3 WHERE id = $1 RETURNING {_FLEET_COLUMNS}"
)

GET_FLEET_FOR_UPDATE = f"SELECT {_FLEET_COLUMNS} FROM fleets WHERE id = $1 FOR UPDATE"

GET_FLEET = f"SELECT {_FLEET_COLUMNS} FROM fleets WHERE id = $1"

LIST_FLEETS = f"SELECT {_FLEET_COLUMNS} FROM fleets ORDER BY created_at"


class FleetStore(PgConnections):
    async def create_fleet(self, name: str) -> Fleet:
        """``FleetExists`` if the name exists, including a racing duplicate."""
        async with self._connection() as conn:
            dup = await conn.fetchrow(SELECT_FLEET_BY_NAME, name)
            if dup is not None:
                raise FleetExists(f"fleet already exists: {name!r}")
            row = await conn.fetchrow(INSERT_FLEET, name)
            if row is None:
                # A concurrent create won the race.
                raise FleetExists(f"fleet already exists: {name!r}")
        return row_to_fleet(row)

    async def list_fleets(self) -> list[Fleet]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_FLEETS)
        return [row_to_fleet(r) for r in rows]

    async def get_fleet(self, fleet_id: str) -> Fleet | None:
        if not is_valid_uuid(fleet_id):
            return None
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_FLEET, fleet_id)
        return row_to_fleet(row) if row is not None else None

    async def rename_fleet(self, fleet_id: str, name: str) -> tuple[Fleet, Fleet]:
        """Rename a fleet (ADR 0109). The row lock makes ``before`` the name the audit row
        reports."""
        if not is_valid_uuid(fleet_id):
            raise UnknownFleet(f"unknown fleet: {fleet_id}")
        try:
            async with self._transaction() as conn:
                row = await conn.fetchrow(GET_FLEET_FOR_UPDATE, fleet_id)
                if row is None:
                    raise UnknownFleet(f"unknown fleet: {fleet_id}")
                before = row_to_fleet(row)
                if before.name == name:
                    return before, before
                if await conn.fetchrow(SELECT_FLEET_BY_NAME, name) is not None:
                    raise FleetExists(f"fleet already exists: {name!r}")
                renamed = await conn.fetchrow(RENAME_FLEET, fleet_id, name)
        except asyncpg.UniqueViolationError as exc:
            raise FleetExists(f"fleet already exists: {name!r}") from exc
        assert renamed is not None  # the row is locked above
        return before, row_to_fleet(renamed)

    async def set_fleet_phase(self, fleet_id: str, phase: FleetPhase | None) -> tuple[Fleet, Fleet]:
        if not is_valid_uuid(fleet_id):
            raise UnknownFleet(f"unknown fleet: {fleet_id}")
        async with self._transaction() as conn:
            row = await conn.fetchrow(GET_FLEET_FOR_UPDATE, fleet_id)
            if row is None:
                raise UnknownFleet(f"unknown fleet: {fleet_id}")
            if row["merged_into"] is not None:
                target = await conn.fetchrow(GET_FLEET, row["merged_into"])
                raise FleetMerged(
                    fleet_id, str(row["merged_into"]), target["name"] if target else None
                )
            updated = await conn.fetchrow(
                SET_FLEET_PHASE,
                fleet_id,
                phase.name.value if phase is not None else None,
                phase.until if phase is not None else None,
            )
        assert updated is not None  # the row is locked above
        return row_to_fleet(row), row_to_fleet(updated)

    async def fleet_facts(self, fleet_id: str) -> FleetFacts:
        if not is_valid_uuid(fleet_id):
            return FleetFacts(agents=0, entries={})
        # One snapshot, so the agent count and the entry states agree.
        async with (
            self._connection() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            agents = await conn.fetchval(FLEET_AGENT_COUNT, fleet_id)
            rows = await conn.fetch(FLEET_ENTRY_STATES, fleet_id)
        return FleetFacts(agents=int(agents), entries={r["state"]: int(r["n"]) for r in rows})
