"""Fleet schemas."""

from __future__ import annotations

from pydantic import BaseModel

from hivemind.domain.access import Fleet


class CreateFleetRequest(BaseModel):
    """Create a named fleet."""

    name: str


class RenameFleetRequest(BaseModel):
    """Give a fleet a new name (ADR 0109)."""

    name: str


class FleetOut(BaseModel):
    """A fleet."""

    id: str
    name: str
    created_at: str
    # Set on a fleet merged into another (ADR 0107); listed only with
    # ``?include_merged=true``.
    merged_into: str | None = None
    merged_at: str | None = None

    @classmethod
    def from_fleet(cls, fleet: Fleet) -> FleetOut:
        return cls(
            id=fleet.id,
            name=fleet.name,
            created_at=fleet.created_at.isoformat(),
            merged_into=fleet.merged_into,
            merged_at=fleet.merged_at.isoformat() if fleet.merged_at is not None else None,
        )
