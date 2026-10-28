"""The raw aggregates behind the admin activity view (ADR 0080).

Counts only, as the ``Store`` returns them; ``ActivityService`` joins them
with the agent and fleet rosters. Observation only: nothing in search,
ranking, scoring, quality or the briefing reads these, and no agent-facing
surface exposes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class AuthorActivity:
    """One (``entries.author``, ``entries.fleet_id``) group: every entry
    written, the ``active`` ones and the ``recent`` ones (``created_at`` at
    or after the window start)."""

    author: str
    fleet_id: str | None
    entries: int
    active: int
    recent: int
    last_write_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReporterActivity:
    """One reporter (``feedbacks."user"``): verdicts given, the ``recent``
    ones (``updated_at`` at or after the window start) and their mix."""

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
    """One UTC day with activity: entries written (``created_at``) and
    verdicts given (``updated_at``)."""

    day: date
    writes: int
    helpful: int
    stale: int
    wrong: int


@dataclass(frozen=True, slots=True)
class Activity:
    """What ``Store.activity`` returns. Verdicts of revoked reporters are
    left out everywhere (ADR 0067), as are ratings an admin cleanup
    set aside (ADR 0111)."""

    authors: tuple[AuthorActivity, ...] = ()
    reporters: tuple[ReporterActivity, ...] = ()
    received: tuple[ReceivedFeedback, ...] = ()
    days: tuple[DayActivity, ...] = ()
