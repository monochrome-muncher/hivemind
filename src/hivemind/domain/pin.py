"""Pinned entries (ADR 0058): a fleet's short briefing.

A privileged agent of a fleet (or an admin) pins a few of the fleet's
entries so every agent of that fleet sees them first when it catches up.
A pin is context, never an order: pinned entries are data like any
other entry (ADR 0043).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from hivemind.domain.entry import Entry


@dataclass(frozen=True, slots=True)
class Pin:
    """One pinned entry of a fleet, and who pinned it when."""

    fleet_id: str
    entry_id: str
    pinned_by: str
    pinned_at: datetime


# Why a pinned entry changed since it was pinned (ADR 0076).
ChangeReason = Literal["superseded", "flagged"]


@dataclass(frozen=True, slots=True)
class PinWrite:
    """What ``Store.pin_entry`` did: the pin as stored, whether the row is
    new, and the ids of the older pins it replaced (a re-pin of the version
    they now show moves the pin; ADR 0076)."""

    pin: Pin
    created: bool
    replaced: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PinnedEntry:
    """A pin as a reader sees it: ``entry`` is the current version of the
    pinned entry (the pin follows supersession, ADR 0058), which may be
    withdrawn. It is ``changed_since_pin`` when the briefing no longer shows
    exactly what was pinned (ADR 0076)."""

    pin: Pin
    entry: Entry
    # ADR 0076: ``"superseded"`` when ``entry`` is a later version than the
    # one pinned, else ``"flagged"`` when a stale or wrong verdict arrived
    # after the pin; ``None`` when neither.
    change_reason: ChangeReason | None = None

    @property
    def changed_since_pin(self) -> bool:
        return self.change_reason is not None
