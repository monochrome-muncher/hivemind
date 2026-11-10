"""Fleet records."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import replace

from hivemind.domain.access import Fleet, FleetExists, UnknownFleet
from hivemind.domain.fleet_merge import FleetFacts
from hivemind.memstore.state import MemoryState


class FleetStore(MemoryState):
    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet (ADR 0011). ``FleetExists`` if the name exists."""
        with self._lock:
            for existing in self._fleets.values():
                if existing.name == name:
                    raise FleetExists(f"fleet already exists: {name!r}")
            fleet = Fleet(id=str(uuid.uuid4()), name=name, created_at=self._clock())
            self._fleets[fleet.id] = fleet
            return fleet

    async def list_fleets(self) -> list[Fleet]:
        with self._lock:
            return list(self._fleets.values())

    async def get_fleet(self, fleet_id: str) -> Fleet | None:
        with self._lock:
            return self._fleets.get(fleet_id)

    async def rename_fleet(self, fleet_id: str, name: str) -> tuple[Fleet, Fleet]:
        with self._lock:
            before = self._fleets.get(fleet_id)
            if before is None:
                raise UnknownFleet(f"unknown fleet: {fleet_id}")
            if before.name == name:
                return before, before
            if any(f.name == name for f in self._fleets.values()):
                raise FleetExists(f"fleet already exists: {name!r}")
            after = replace(before, name=name)
            self._fleets[fleet_id] = after
            return before, after

    async def fleet_facts(self, fleet_id: str) -> FleetFacts:
        with self._lock:
            agents = sum(1 for a in self._agents.values() if a.home_fleet_id == fleet_id)
            states = Counter(
                e.state.value for e in self._entries.values() if e.fleet_id == fleet_id
            )
        return FleetFacts(agents=agents, entries=dict(states))
