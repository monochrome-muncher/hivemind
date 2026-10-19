"""Pinned entries (ADR 0058): a fleet's short briefing.

A contributor or privileged agent of a fleet (or an admin) pins a few of
the fleet's entries so every agent of that fleet sees them first when it catches up.
A pin is context, never an order: pinned entries are data like any
other entry (ADR 0043).
"""

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
    """What ``Store.pin_entry`` did: the pin as stored, whether the row is
    new, and the ids of the older pins it replaced (a re-pin of the version
    they now show moves the pin; ADR 0076)."""

    pin: Pin
    created: bool
    replaced: tuple[str, ...] = ()


class PinRefusal(StrEnum):
    """Which cap ``Store.pin_entry`` hit (ADR 0096)."""

    FLEET = "fleet"  # the fleet would hold more than its limit
    AGENT = "agent"  # the pinner would hold more than the per-agent limit


def pin_refusal(
    pins: Iterable[Pin],
    entry_id: str,
    pinned_by: str,
    limit: int,
    agent_limit: int | None,
    replaces: Sequence[str],
) -> PinRefusal | None:
    """The cap a pin would break, given the fleet's current ``pins``, or
    ``None``. Shared by both stores so they agree (ADR 0096).

    The fleet cap only bites when the pin adds a row: pinning a pinned entry
    (a refresh) or moving older pins onto ``entry_id`` never grows the
    count. The per-agent cap counts the pins ``pinned_by`` would hold
    afterwards: its other pins, less those ``replaces`` removes, plus this
    one, since pinning stamps ``pinned_by`` even on a refresh. ``None``
    ``agent_limit`` (an admin) is exempt."""
    current = list(pins)
    replaced = {old for old in replaces if old != entry_id}
    existing = any(p.entry_id == entry_id for p in current)
    removed = {p.entry_id for p in current if p.entry_id in replaced}
    if not existing and not removed and len(current) >= limit:
        return PinRefusal.FLEET
    if agent_limit is not None:
        kept = sum(
            1
            for p in current
            if p.pinned_by == pinned_by and p.entry_id != entry_id and p.entry_id not in removed
        )
        if kept + 1 > agent_limit:
            return PinRefusal.AGENT
    return None


@dataclass(frozen=True, slots=True)
class PinnedEntry:
    """A pin as a reader sees it: ``entry`` is the current version of the
    pinned entry (the pin follows supersession, ADR 0058). It is ``changed_since_pin`` when the briefing no longer shows
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
