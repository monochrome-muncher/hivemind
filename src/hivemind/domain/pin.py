"""Pinned entries (ADR 0058): a fleet's short briefing. A pin is context, never an order (ADR
0043)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
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
    """What ``Store.pin_entry`` did: the pin, whether the row is new, and the older pins it
    replaced (ADR 0076)."""

    pin: Pin
    created: bool
    replaced: tuple[str, ...] = ()


class PinRefusal(StrEnum):
    """Which cap ``Store.pin_entry`` hit (ADR 0096)."""

    FLEET = "fleet"
    AGENT = "agent"


def pin_refusal(
    pins: Iterable[Pin],
    entry_id: str,
    pinned_by: str,
    limit: int,
    agent_limit: int | None,
    replaces: Sequence[str],
    also_owned_by: Sequence[str] = (),
) -> PinRefusal | None:
    """The cap a pin would break, given the fleet's ``pins``, or ``None``; shared by both stores
    (ADR 0096).

    The fleet cap bites only when the pin adds a row (a refresh or replacing pins never does).
    The per-agent cap counts ``pinned_by``'s other pins less those ``replaces`` removes, plus
    this one (a refresh restamps ``pinned_by``); ``None`` ``agent_limit`` (admin) is exempt.
    Pins by ``also_owned_by`` (ADR 0113) use up ``pinned_by``'s share but are never dropped."""
    current = list(pins)
    replaced = {old for old in replaces if old != entry_id}
    existing = any(p.entry_id == entry_id for p in current)
    removed = {p.entry_id for p in current if p.entry_id in replaced}
    if not existing and not removed and len(current) >= limit:
        return PinRefusal.FLEET
    if agent_limit is not None:
        owners = {pinned_by, *also_owned_by}
        kept = sum(
            1
            for p in current
            if p.pinned_by in owners and p.entry_id != entry_id and p.entry_id not in removed
        )
        if kept + 1 > agent_limit:
            return PinRefusal.AGENT
    return None


@dataclass(frozen=True, slots=True)
class PinnedEntry:
    """A pin as a reader sees it: ``entry`` is the current version (the pin follows supersession,
    ADR 0058); ``changed_since_pin`` when that is no longer exactly what was pinned (ADR 0076)."""

    pin: Pin
    entry: Entry
    # ADR 0076: ``"superseded"`` if ``entry`` is a later version, else ``"flagged"`` if a
    # stale/wrong verdict came after the pin.
    change_reason: ChangeReason | None = None

    @property
    def changed_since_pin(self) -> bool:
        return self.change_reason is not None
