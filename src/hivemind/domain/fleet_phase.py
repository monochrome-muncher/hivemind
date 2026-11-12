"""Fleet phases (ADR 0143): an admin sets a fleet to explore or consolidate until a date.

The phase only changes the hint its agents read in ``hive_whoami`` and ``hive_pinned``. It is
server text, never an entry, and it never touches ranking or search. A phase whose end has
passed counts as none; nothing clears it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from hivemind.domain.validation import InvalidInput

MAX_PHASE_DAYS = 30
# A caller's clock may run a little ahead of the server's.
CLOCK_GRACE = timedelta(minutes=1)


class PhaseName(StrEnum):
    EXPLORE = "explore"
    CONSOLIDATE = "consolidate"


_HINTS: dict[PhaseName, str] = {
    PhaseName.EXPLORE: (
        "Your fleet is exploring until {date}. On open-ended work, try explore: true, "
        "write untested approaches as their own entries tagged alternative, write dead ends "
        "and open questions, and when the stakes are low try the less-tested of two rivals "
        "and report what you found."
    ),
    PhaseName.CONSOLIDATE: (
        "Your fleet is consolidating until {date}. Before new work, settle what is open: "
        "test alternative and Conflict: entries and supersede or flag them, distill clusters "
        "of three or more entries, and fix flagged entries."
    ),
}


@dataclass(frozen=True, slots=True)
class FleetPhase:
    """A phase and the moment it ends (UTC)."""

    name: PhaseName
    until: datetime

    @classmethod
    def starting(cls, kind: PhaseName, until: datetime, now: datetime) -> FleetPhase:
        """A phase beginning ``now``; ``InvalidInput`` unless ``until`` is later than ``now`` and
        at most ``MAX_PHASE_DAYS`` days ahead (plus a minute of clock grace). A naive ``until`` means UTC."""
        until = until.replace(tzinfo=UTC) if until.tzinfo is None else until.astimezone(UTC)
        if until <= now:
            raise InvalidInput("until must be in the future")
        if until > now + timedelta(days=MAX_PHASE_DAYS) + CLOCK_GRACE:
            raise InvalidInput(f"until must be at most {MAX_PHASE_DAYS} days ahead")
        return cls(kind, until)

    def in_force(self, now: datetime) -> bool:
        return self.until > now

    @property
    def hint(self) -> str:
        return _HINTS[self.name].format(date=self.until.date().isoformat())

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape of ``fleet_phase`` in ``hive_whoami`` and ``hive_pinned``."""
        return {"phase": self.name.value, "until": self.until.isoformat(), "hint": self.hint}


__all__ = ["MAX_PHASE_DAYS", "FleetPhase", "PhaseName"]
