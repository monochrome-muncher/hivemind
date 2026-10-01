"""Pinned entries (ADR 0058): a fleet's short briefing.

A privileged agent of a fleet (or an admin) pins a few of the fleet's
entries so every agent of that fleet sees them first when it catches up.
A pin is context, never an order: pinned entries are data like any
other entry (ADR 0043).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from hivemind.domain.entry import Entry


@dataclass(frozen=True, slots=True)
class Pin:
    """One pinned entry of a fleet, and who pinned it when."""

    fleet_id: str
    entry_id: str
    pinned_by: str
    pinned_at: datetime


@dataclass(frozen=True, slots=True)
class PinnedEntry:
    """A pin as a reader sees it: ``entry`` is the current version of the
    pinned entry (the pin follows supersession, ADR 0058), which may be
    withdrawn."""

    pin: Pin
    entry: Entry
