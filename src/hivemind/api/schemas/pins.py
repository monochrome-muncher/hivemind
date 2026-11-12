"""Pin schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from hivemind.api.schemas.fleets import FleetPhaseOut
from hivemind.domain.entry import EntryState, Kind
from hivemind.domain.pin import ChangeReason, Pin, PinnedEntry


class PinOut(BaseModel):
    """A pin: which entry is pinned to which fleet, by whom."""

    entry_id: str
    fleet_id: str
    pinned_by: str
    pinned_at: datetime

    @classmethod
    def from_pin(cls, pin: Pin) -> PinOut:
        return cls(
            entry_id=pin.entry_id,
            fleet_id=pin.fleet_id,
            pinned_by=pin.pinned_by,
            pinned_at=pin.pinned_at,
        )


class UnpinOut(BaseModel):
    """The answer to an unpin: whether the entry was pinned."""

    entry_id: str
    removed: bool


class PinnedOut(BaseModel):
    """One pinned entry as the reader sees it: ``id`` is the
    current version, ``pinned_id`` the version that was pinned."""

    id: str
    pinned_id: str
    kind: Kind
    summary: str
    author: str
    state: EntryState
    pinned_by: str
    pinned_at: datetime
    # ``change_reason`` is ``superseded`` or ``flagged`` when set.
    changed_since_pin: bool
    change_reason: ChangeReason | None

    @classmethod
    def from_pinned(cls, pinned: PinnedEntry) -> PinnedOut:
        return cls(
            id=pinned.entry.id,
            pinned_id=pinned.pin.entry_id,
            kind=pinned.entry.kind,
            summary=pinned.entry.summary,
            author=pinned.entry.author,
            state=pinned.entry.state,
            pinned_by=pinned.pin.pinned_by,
            pinned_at=pinned.pin.pinned_at,
            changed_since_pin=pinned.changed_since_pin,
            change_reason=pinned.change_reason,
        )


class PinsOut(BaseModel):
    """A fleet's pinned entries, newest pin first."""

    fleet_id: str | None
    pins: list[PinnedOut]
    fleet_phase: FleetPhaseOut | None = None
