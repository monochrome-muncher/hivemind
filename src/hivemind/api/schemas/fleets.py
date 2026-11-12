"""Fleet schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from hivemind.domain.access import Fleet
from hivemind.domain.fleet_phase import FleetPhase, PhaseName
from hivemind.domain.validation import utcnow


class CreateFleetRequest(BaseModel):
    """Create a named fleet."""

    name: str


class RenameFleetRequest(BaseModel):
    """Give a fleet a new name (ADR 0109)."""

    name: str


class SetFleetPhaseRequest(BaseModel):
    """Put a fleet in a phase until a moment at most 30 days ahead (ADR 0143)."""

    phase: PhaseName
    until: datetime


class FleetPhaseOut(BaseModel):
    """A phase in force, as ``hive_whoami`` and ``hive_pinned`` tell it (ADR 0143)."""

    phase: PhaseName
    until: str
    hint: str

    @classmethod
    def from_phase(cls, phase: FleetPhase) -> FleetPhaseOut:
        return cls.model_validate(phase.as_dict())


class FleetOut(BaseModel):
    """A fleet."""

    id: str
    name: str
    created_at: str
    # Set on a fleet merged into another (ADR 0107); listed only with
    # ``?include_merged=true``.
    merged_into: str | None = None
    merged_at: str | None = None
    # The phase in force (ADR 0143); null once it has ended.
    phase: PhaseName | None = None
    phase_until: str | None = None

    @classmethod
    def from_fleet(cls, fleet: Fleet) -> FleetOut:
        phase = fleet.phase_in_force(utcnow())
        return cls(
            id=fleet.id,
            name=fleet.name,
            created_at=fleet.created_at.isoformat(),
            merged_into=fleet.merged_into,
            merged_at=fleet.merged_at.isoformat() if fleet.merged_at is not None else None,
            phase=phase.name if phase is not None else None,
            phase_until=phase.until.isoformat() if phase is not None else None,
        )
