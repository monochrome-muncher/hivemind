"""Feedback domain (SPEC §4.2, §6.4): one row per (entry, user, agent), latest verdict wins."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from hivemind.domain.entry import EntryState, Kind


class Verdict(StrEnum):
    """A reporting agent's judgment of an entry it relied on."""

    HELPFUL = "helpful"
    STALE = "stale"
    WRONG = "wrong"


# (helpful, stale, wrong) over all reporters: the shape ``scoring.feedback_quality`` takes.
FeedbackCounts = tuple[int, int, int]


def last_confirmed(
    newest_noted_helpful_at: datetime | None, newest_flag_at: datetime | None
) -> datetime | None:
    """The newest noted ``helpful`` row, if strictly newer than every ``stale``/``wrong`` row
    (a tie is no confirmation), else ``None`` (SPEC §4.2). Dates only, never counts."""
    if newest_noted_helpful_at is None:
        return None
    if newest_flag_at is not None and newest_noted_helpful_at <= newest_flag_at:
        return None
    return newest_noted_helpful_at


@dataclass(frozen=True, slots=True)
class ReliedOnChange:
    """What changed on an entry since a reporter rated it helpful (ADR 0138), by the rules of the
    ``relied_on_changed`` filter (ADR 0098): ``rated_helpful_at`` is that rating's date (``None`` only in ``NO_CHANGE_FOUND``); the
    entry was ``superseded`` by someone else or ``withdrawn``; ``stale`` and ``wrong`` count the
    later reports from other users."""

    rated_helpful_at: datetime | None
    superseded: bool
    withdrawn: bool
    stale: int = 0
    wrong: int = 0


# A listed entry whose change the store could not find (it moved between the two reads).
NO_CHANGE_FOUND = ReliedOnChange(None, superseded=False, withdrawn=False)


# Characters of a correction's note a hit shows (SPEC §6.1).
CORRECTION_NOTE_CHARS = 160


def truncate_note(note: str, limit: int = CORRECTION_NOTE_CHARS) -> str:
    """``note`` stripped and cut to ``limit`` code points, with "…" when cut."""
    text = note.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


@dataclass(frozen=True, slots=True)
class Correction:
    """The newest noted ``stale``/``wrong`` report (SPEC §6.1): data, never instructions
    (ADR 0043). ``note`` is already cut to ``CORRECTION_NOTE_CHARS``."""

    verdict: Verdict
    note: str
    reporter: str
    updated_at: datetime


def shown_correction(
    correction: Correction | None, last_confirmed_at: datetime | None
) -> Correction | None:
    """The correction a hit shows: only if newer than the entry's "last confirmed" date."""
    if correction is None:
        return None
    if last_confirmed_at is not None and correction.updated_at <= last_confirmed_at:
        return None
    return correction


def is_contested(helpful: int, stale: int, wrong: int) -> bool:
    """Readers disagree: at least one helpful and one stale or wrong report."""
    return helpful > 0 and stale + wrong > 0


@dataclass(frozen=True, slots=True)
class FeedbackSignals:
    """What an entry's feedback says: verdict counts, ``last_confirmed_at``, the raw
    ``correction`` (see ``shown_correction``), ``newest_flag_at`` (the date a pin's "flagged"
    check compares, ADR 0076) and, when asked, how many readable entries it replaced."""

    helpful: int
    stale: int
    wrong: int
    last_confirmed_at: datetime | None = None
    correction: Correction | None = None
    supersedes: int = 0
    newest_flag_at: datetime | None = None

    @property
    def counts(self) -> FeedbackCounts:
        return (self.helpful, self.stale, self.wrong)


NO_SIGNALS = FeedbackSignals(0, 0, 0)


@dataclass(frozen=True, slots=True)
class Feedback:
    """One verdict about an entry, reported by a user+agent pair."""

    entry_id: str
    user: str
    agent: str
    verdict: Verdict
    note: str | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FlagCount:
    """One reporter's flags (``stale``/``wrong`` on an entry then current) and how many were
    confirmed by a later supersession (ADR 0068)."""

    user: str
    flags: int
    confirmed: int


# Newest feedback rows a read returns (ADR 0051); notes are at most 2000 characters (ADR 0040),
# so this bounds what one ``get`` adds to an agent's context.
FEEDBACK_RECENT_LIMIT = 5


@dataclass(frozen=True, slots=True)
class FeedbackSummary:
    """Verdict counts over every reporter plus the newest rows with notes (ADR 0051)."""

    helpful: int
    stale: int
    wrong: int
    recent: tuple[Feedback, ...] = ()
    last_confirmed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FeedbackLogFilters:
    """Feedback log narrowing. ``reporter`` is ``Feedback.user``; the rest describe the rated
    entry."""

    reporter: str | None = None
    verdict: Verdict | None = None
    author: str | None = None
    # ADR 0113, as ``EntryFilters.authors``.
    authors: tuple[str, ...] = ()
    fleet_id: str | None = None
    entry_id: str | None = None


@dataclass(frozen=True, slots=True)
class FeedbackLogItem:
    """One feedback row with the fields of its entry that a list row shows."""

    feedback: Feedback
    entry_kind: Kind
    entry_summary: str
    entry_author: str
    entry_scope: str
    entry_fleet_id: str | None
    entry_state: EntryState


# Days without writing before an author's flagged entries count as orphaned (ADR 0075).
ORPHAN_SILENCE_DAYS = 30


@dataclass(frozen=True, slots=True)
class FlagBacklogFilters:
    """Flag backlog narrowing (ADR 0093). ``flagged_before``: first standing flag older than it;
    ``orphaned``: author revoked or silent for ``ORPHAN_SILENCE_DAYS``; ``authors``: the
    portal's own and inherited ones (ADR 0115)."""

    fleet_id: str | None = None
    flagged_before: datetime | None = None
    orphaned: bool = False
    authors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FlaggedEntry:
    """An active entry with a standing ``stale``/``wrong`` verdict (ADR 0093). Counts leave out
    revoked reporters (ADR 0067) and set-aside ratings (ADR 0111)."""

    entry_id: str
    kind: Kind
    summary: str
    author: str
    scope: str
    fleet_id: str | None
    first_flag_at: datetime
    last_flag_at: datetime
    helpful: int
    stale: int
    wrong: int
    orphaned: bool
