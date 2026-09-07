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


@dataclass(frozen=True, slots=True)
class Feedback:
    """One verdict about an entry, reported by a user+agent pair."""

    entry_id: str
    user: str
    agent: str
    verdict: Verdict
    note: str | None = None
    updated_at: datetime | None = None
