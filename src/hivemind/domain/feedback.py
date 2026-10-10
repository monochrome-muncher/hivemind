"""Feedback domain (SPEC.md §4.2): the dumb outcome loop.

One feedback row per (entry, user, agent) with upsert semantics —
the reporter's latest verdict wins. Verdicts feed a bounded quality
multiplier that retrieval re-scores on; there is no learned tuning in
v1 (SPEC.md §4.2, §6.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Verdict(StrEnum):
    """A reporting agent's judgment of an entry it relied on."""

    HELPFUL = "helpful"
    STALE = "stale"
    WRONG = "wrong"


# Counts of each verdict over all reporters, in (helpful, stale, wrong)
# order — the shape scoring.feedback_quality consumes.
FeedbackCounts = tuple[int, int, int]


def last_confirmed(
    newest_noted_helpful_at: datetime | None, newest_flag_at: datetime | None
) -> datetime | None:
    """An entry's "last confirmed" date (SPEC.md §4.2): the newest
    ``helpful`` row that carries a note, provided it is strictly newer than
    every ``stale`` or ``wrong`` row; ``None`` otherwise (a tie is no
    confirmation). Dates only: how many confirmed never matters."""
    if newest_noted_helpful_at is None:
        return None
    if newest_flag_at is not None and newest_noted_helpful_at <= newest_flag_at:
        return None
    return newest_noted_helpful_at


@dataclass(frozen=True, slots=True)
class FeedbackSignals:
    """What the feedback on one entry says: the verdict counts over every
    reporter and the "last confirmed" date (see ``last_confirmed``)."""

    helpful: int
    stale: int
    wrong: int
    last_confirmed_at: datetime | None = None

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
    """One reporter's flags and how many were later confirmed (ADR 0068).

    A flag is a ``stale`` or ``wrong`` row on an entry that was still
    current when the row was written; it is confirmed when the entry was
    superseded after the row. ``user`` is the registered reporter name.
    """

    user: str
    flags: int
    confirmed: int


# How many of an entry's feedback rows a read returns (ADR 0051): the
# newest ones. Each note is at most 2000 characters (ADR 0040), so this
# bounds what one ``get`` adds to an agent's context.
FEEDBACK_RECENT_LIMIT = 5


@dataclass(frozen=True, slots=True)
class FeedbackSummary:
    """What a reader sees of an entry's feedback (ADR 0051): the verdict
    counts over every reporter, plus the newest rows with their notes."""

    helpful: int
    stale: int
    wrong: int
    recent: tuple[Feedback, ...] = ()
    last_confirmed_at: datetime | None = None
