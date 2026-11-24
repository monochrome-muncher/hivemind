"""Raw aggregates behind the admin activity view (ADR 0080). Observation only: nothing in
retrieval reads them and no agent-facing surface exposes them."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class AuthorActivity:
    """One (author, fleet_id) group: entries written, ``active`` and ``recent`` (in the window)."""

    author: str
    fleet_id: str | None
    entries: int
    active: int
    recent: int
    last_write_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReporterActivity:
    """One reporter: verdicts given, the ``recent`` ones (in the window) and their mix."""

    reporter: str
    verdicts: int
    recent: int
    helpful: int
    stale: int
    wrong: int
    last_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReceivedFeedback:
    """The verdicts on one author's entries, by kind."""

    author: str
    helpful: int
    stale: int
    wrong: int


@dataclass(frozen=True, slots=True)
class DayActivity:
    """One UTC day: entries written and verdicts given."""

    day: date
    writes: int
    helpful: int
    stale: int
    wrong: int


@dataclass(frozen=True, slots=True)
class Activity:
    """What ``Store.activity`` returns; leaves out revoked reporters (ADR 0067) and set-aside
    ratings (ADR 0111)."""

    authors: tuple[AuthorActivity, ...] = ()
    reporters: tuple[ReporterActivity, ...] = ()
    received: tuple[ReceivedFeedback, ...] = ()
    days: tuple[DayActivity, ...] = ()


@dataclass(frozen=True, slots=True)
class AgentDayActivity:
    """One agent on one UTC day: memories it wrote and verdicts it gave."""

    agent: str
    day: date
    writes: int
    ratings: int


@dataclass(frozen=True, slots=True)
class FleetActivity:
    """What ``Store.fleet_activity`` returns: a row for each (member, day) with any activity and
    how many active members had none in the window."""

    days: tuple[AgentDayActivity, ...] = ()
    quiet: int = 0
