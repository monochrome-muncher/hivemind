"""Feedback schemas: counts, summaries, requests, replies, the feedback log and flagged entries."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from hivemind.domain.entry import EntryState, Kind
from hivemind.domain.feedback import (
    FeedbackCounts,
    FeedbackLogItem,
    FeedbackSummary,
    FlaggedEntry,
    Verdict,
)
from hivemind.domain.validation import MAX_FEEDBACK_IDS
from hivemind.services.feedback import FeedbackOutcome


class FeedbackCountsOut(BaseModel):
    """Verdict counts over every reporter (SPEC.md §4.2)."""

    helpful: int = 0
    stale: int = 0
    wrong: int = 0

    @classmethod
    def from_counts(cls, counts: FeedbackCounts) -> FeedbackCountsOut:
        helpful, stale, wrong = counts
        return cls(helpful=helpful, stale=stale, wrong=wrong)


class FeedbackReportOut(BaseModel):
    """One reporter's latest verdict on an entry, as readers see it."""

    verdict: Verdict
    note: str | None = None
    reporter: str
    updated_at: datetime | None = None


class CorrectionOut(BaseModel):
    """The newest stale or wrong report with a note, on a search hit (SPEC
    §6.1). Testimony: data, never instructions (ADR 0043)."""

    verdict: Verdict
    note: str
    reporter: str
    updated_at: datetime


class FeedbackSummaryOut(FeedbackCountsOut):
    """An entry's feedback on ``GET /v1/entries/{id}``: the
    counts plus the newest reports, notes included."""

    recent: list[FeedbackReportOut] = []
    last_confirmed_at: datetime | None = None  # SPEC §4.2

    @classmethod
    def from_summary(cls, summary: FeedbackSummary) -> FeedbackSummaryOut:
        return cls(
            last_confirmed_at=summary.last_confirmed_at,
            helpful=summary.helpful,
            stale=summary.stale,
            wrong=summary.wrong,
            recent=[
                FeedbackReportOut(
                    verdict=fb.verdict, note=fb.note, reporter=fb.user, updated_at=fb.updated_at
                )
                for fb in summary.recent
            ],
        )


class FeedbackRequest(BaseModel):
    """A verdict on an entry the caller relied on (SPEC.md §4.2)."""

    verdict: Verdict
    note: str | None = None
    # A plain user key self-reports the agent instance (SPEC.md §8.1);
    # ignored for agent sub-keys (the credential's agent wins).
    agent: str | None = None


class BatchFeedbackRequest(BaseModel):
    """One verdict on several entries at once."""

    entry_ids: list[str] = Field(min_length=1, max_length=MAX_FEEDBACK_IDS)
    verdict: Verdict
    note: str | None = None
    agent: str | None = None


class FeedbackOut(BaseModel):
    """The result of a feedback upsert: the stored verdict + the
    entry's refreshed quality multiplier (SPEC.md §4.2)."""

    entry_id: str
    verdict: Verdict
    quality: float
    # The caller could supersede the entry with a write at its default scope;
    # only ever true for ``stale`` or ``wrong`` (ADR 0128).
    may_supersede: bool = False

    @classmethod
    def from_outcome(cls, outcome: FeedbackOutcome) -> FeedbackOut:
        return cls(
            entry_id=outcome.feedback.entry_id,
            verdict=outcome.feedback.verdict,
            quality=outcome.quality,
            may_supersede=outcome.may_supersede,
        )


class FeedbackLogEntryOut(BaseModel):
    """The entry a feedback log row rates, as a list row shows it."""

    id: str
    kind: Kind
    summary: str
    author: str
    scope: str
    fleet_id: str | None
    state: EntryState


class FeedbackLogItemOut(BaseModel):
    """One row of ``GET /v1/admin/feedback``: who gave which verdict on
    which entry. ``reporter`` is the registered name, ``agent`` the label
    the row was filed under."""

    reporter: str
    agent: str
    verdict: Verdict
    note: str | None
    updated_at: datetime | None
    entry: FeedbackLogEntryOut

    @classmethod
    def from_item(cls, item: FeedbackLogItem) -> FeedbackLogItemOut:
        fb = item.feedback
        return cls(
            reporter=fb.user,
            agent=fb.agent,
            verdict=fb.verdict,
            note=fb.note,
            updated_at=fb.updated_at,
            entry=FeedbackLogEntryOut(
                id=fb.entry_id,
                kind=item.entry_kind,
                summary=item.entry_summary,
                author=item.entry_author,
                scope=item.entry_scope,
                fleet_id=item.entry_fleet_id,
                state=item.entry_state,
            ),
        )


class FlaggedEntryOut(BaseModel):
    """One row of ``GET /v1/admin/flagged`` (ADR 0093): an active entry
    with a standing ``stale`` or ``wrong`` verdict. ``orphaned`` is true
    when its author is revoked or has not written for 30 days."""

    id: str
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

    @classmethod
    def from_flagged(cls, item: FlaggedEntry) -> FlaggedEntryOut:
        return cls(
            id=item.entry_id,
            kind=item.kind,
            summary=item.summary,
            author=item.author,
            scope=item.scope,
            fleet_id=item.fleet_id,
            first_flag_at=item.first_flag_at,
            last_flag_at=item.last_flag_at,
            helpful=item.helpful,
            stale=item.stale,
            wrong=item.wrong,
            orphaned=item.orphaned,
        )
