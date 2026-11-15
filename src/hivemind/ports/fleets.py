"""Fleets and their phase."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hivemind.domain.access import (
    Fleet,
)
from hivemind.domain.fleet_merge import (
    FleetFacts,
)
from hivemind.domain.fleet_phase import FleetPhase


@runtime_checkable
class FleetPort(Protocol):
    """Fleets and their phase."""

    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet. ``FleetExists`` if the name already exists."""
        ...

    async def list_fleets(self) -> list[Fleet]:
        """Every fleet, merged ones included (``Fleet.merged_into``)."""
        ...

    async def get_fleet(self, fleet_id: str) -> Fleet | None: ...

    async def rename_fleet(self, fleet_id: str, name: str) -> tuple[Fleet, Fleet]:
        """Rename a fleet, merged ones included; the id never changes (ADR 0109).

        Returns the fleet before and after (the same object, nothing written, when ``name`` is
        unchanged). ``UnknownFleet``; ``FleetExists`` when another fleet has the name."""
        ...

    async def set_fleet_phase(self, fleet_id: str, phase: FleetPhase | None) -> tuple[Fleet, Fleet]:
        """Replace the fleet's phase, or clear it with ``None`` (ADR 0143); the fleet before and
        after. ``UnknownFleet``; ``FleetMerged`` for a merged fleet, nothing written. A merge
        clears the source's phase, so an undo does not bring it back."""
        ...

    async def fleet_facts(self, fleet_id: str) -> FleetFacts:
        """Agents whose home fleet is ``fleet_id`` and its entry counts by state (an empty state is
        absent)."""
        ...
