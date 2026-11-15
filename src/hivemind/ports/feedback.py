"""Feedback on entries and the signals derived from it."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from hivemind.domain.access import (
    Visibility,
)
from hivemind.domain.feedback import (
    Feedback,
    FeedbackCounts,
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSignals,
    FlagBacklogFilters,
    FlagCount,
    FlaggedEntry,
    ReliedOnChange,
)


@runtime_checkable
class FeedbackPort(Protocol):
    """Feedback on entries and the signals derived from it."""

    async def flag_counts(self) -> list[FlagCount]:
        """Per reporter (the ``"user"`` column): ``stale``/``wrong`` rows written while the entry
        was current, and how many of those entries were superseded after the row (ADR 0068).

        Left out: rows on already superseded entries, revoked reporters (ADR 0067), set-aside
        ratings (ADR 0111). A reporter with no flags has no row; any order."""
        ...

    async def record_feedback(self, feedback: Feedback) -> Feedback:
        """Upsert a verdict (one row per entry+user+agent); returns the row as stored.

        No ``updated_at`` is stamped with the store clock. Rating again a memory whose rating a
        cleanup set aside clears the mark (ADR 0111)."""
        ...

    async def record_feedback_many(self, feedbacks: list[Feedback]) -> list[Feedback]:
        """Upsert several rows in one transaction, all or none (ADR 0079); returns them as stored,
        in order. Rows without ``updated_at`` share one store-clock stamp. Empty is a no-op."""
        ...

    async def feedback_counts(self, entry_id: str) -> FeedbackCounts:
        """(helpful, stale, wrong) counts. Every feedback read leaves out revoked reporters (ADR
        0067) and set-aside rows (ADR 0111)."""
        ...

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, FeedbackCounts]:
        """Batched counts: every well-formed UUID requested is a key, ``(0, 0, 0)`` if none;
        malformed ids are left out."""
        ...

    async def relied_on_changes(
        self, entry_ids: list[str], reporter: tuple[str, str]
    ) -> dict[str, ReliedOnChange]:
        """For each of ``entry_ids`` the ``reporter`` (user, agent) rated helpful, what changed
        since (ADR 0138), judged as the ``relied_on_changed`` filter does (ADR 0098), in one
        read. Entries it did not rate helpful, malformed ids and unknown ids are left out."""
        ...

    async def feedback_signals(
        self,
        entry_ids: list[str],
        *,
        count_supersedes: bool = False,
        visibility: Visibility | None = None,
    ) -> dict[str, FeedbackSignals]:
        """``quality_counts`` plus ``last_confirmed_at`` (SPEC §4.2), ``newest_flag_at`` (newest
        stale or wrong row, noted or not) and ``correction`` (newest stale or wrong report with a
        non-empty note, cut by ``truncate_note``; SPEC §6.1), in one read.

        Every well-formed UUID requested is a key (zero counts, ``None``s if none); malformed ids
        are left out. Revoked reporters are excluded. With ``count_supersedes``, ``supersedes``
        counts entries naming the id in ``superseded_by`` that ``visibility`` may read; otherwise
        0."""
        ...

    async def list_feedback(self, entry_id: str, limit: int) -> list[Feedback]:
        """An entry's feedback rows, at most ``limit``: informative ones first (stale or wrong, or a
        note), then bare ``helpful`` (ADR 0064); each group newest ``updated_at`` first, then
        reporter.

        No visibility check: the caller has established the entry is readable."""
        ...

    async def list_feedback_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, list[Feedback]]:
        """``list_feedback`` for several entries in one read (ADR 0059); every requested id is a key
        (``[]`` if none)."""
        ...

    async def list_feedback_log(
        self,
        filters: FeedbackLogFilters,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FeedbackLogItem]:
        """Feedback rows across entries matching ``filters``, each with its entry: newest
        ``updated_at`` first, then entry id, reporter, agent.

        Only rows whose entry ``visibility`` may read; revoked reporters (ADR 0067) and set-aside
        ratings (ADR 0111) are left out. A malformed ``entry_id`` or ``fleet_id`` matches
        nothing."""
        ...

    async def list_flag_backlog(
        self,
        filters: FlagBacklogFilters,
        now: datetime,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FlaggedEntry]:
        """Active entries with a standing ``stale`` or ``wrong`` verdict (ADR 0093), oldest flag
        first, then entry id.

        Revoked reporters (ADR 0067) and set-aside rows (ADR 0111) are left out, so an entry only
        they flagged is not listed. ``orphaned`` at ``now``: the author is revoked or wrote nothing
        in the ``ORPHAN_SILENCE_DAYS`` before it (as ``fleet_health``). Only entries ``visibility``
        may read; a malformed ``fleet_id`` matches nothing."""
        ...
