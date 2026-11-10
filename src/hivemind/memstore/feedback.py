"""Feedback ratings and their readers."""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta

from hivemind.domain.access import AgentStatus, Visibility
from hivemind.domain.entry import EntryState
from hivemind.domain.feedback import (
    ORPHAN_SILENCE_DAYS,
    Correction,
    Feedback,
    FeedbackCounts,
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSignals,
    FlagBacklogFilters,
    FlagCount,
    FlaggedEntry,
    Verdict,
    last_confirmed,
    truncate_note,
)
from hivemind.domain.validation import canonical_uuid, is_valid_uuid
from hivemind.memstore.helpers import EPOCH, canonical_or_same, count_for
from hivemind.memstore.state import MemoryState


def _canonical_ids(ids: list[str]) -> list[str]:
    """The distinct well-formed ids in canonical form, in order, as PgStore keys them."""
    return list(dict.fromkeys(c for i in ids if (c := canonical_uuid(i)) is not None))


def _signals_for(feedback: dict[tuple[str, str, str], Feedback], entry_id: str) -> FeedbackSignals:
    helpful, stale, wrong = count_for(feedback, entry_id)
    noted: list[datetime] = []
    flags: list[datetime] = []
    flagged: list[Feedback] = []
    flagged_notes: list[Feedback] = []
    for fb in feedback.values():
        if fb.entry_id != entry_id:
            continue
        if fb.verdict is Verdict.HELPFUL:
            if fb.note and fb.note.strip() and fb.updated_at is not None:
                noted.append(fb.updated_at)
        else:
            flags.append(fb.updated_at or EPOCH)
            flagged.append(fb)
            if fb.note and fb.note.strip():
                flagged_notes.append(fb)
    correction = None
    if flagged_notes:
        # Newest first, then reporter and agent: Postgres's ORDER BY.
        flagged_notes.sort(key=lambda fb: (fb.user, fb.agent))
        flagged_notes.sort(key=lambda fb: fb.updated_at or EPOCH, reverse=True)
        newest = flagged_notes[0]
        correction = Correction(
            newest.verdict,
            truncate_note(newest.note or ""),
            newest.user,
            newest.updated_at or EPOCH,
        )
    return FeedbackSignals(
        helpful,
        stale,
        wrong,
        last_confirmed(max(noted, default=None), max(flags, default=None)),
        correction,
        newest_flag_at=max(
            (fb.updated_at for fb in flagged if fb.updated_at is not None), default=None
        ),
    )


class FeedbackStore(MemoryState):
    async def record_feedback(self, feedback: Feedback) -> Feedback:
        (stored,) = await self.record_feedback_many([feedback])
        return stored

    async def record_feedback_many(self, feedbacks: list[Feedback]) -> list[Feedback]:
        with self._lock:
            now = self._clock()  # one stamp per call, like a transaction's now()
            stored = [replace(fb, updated_at=fb.updated_at or now) for fb in feedbacks]
            for fb in stored:
                # Kept under the canonical id, as Postgres's uuid column reads
                # back; the caller gets its own spelling returned.
                canonical = canonical_or_same(fb.entry_id)
                key = (canonical, fb.user, fb.agent)
                self._feedback[key] = replace(fb, entry_id=canonical)
                self._set_aside.pop(key, None)  # rating again clears the mark (ADR 0111)
            return stored

    async def feedback_counts(self, entry_id: str) -> FeedbackCounts:
        if not is_valid_uuid(entry_id):
            return (0, 0, 0)
        with self._lock:
            return count_for(self._live_feedback(), str(uuid.UUID(entry_id)))

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, FeedbackCounts]:
        with self._lock:
            # Every well-formed id, known or not, keyed canonically, as on PgStore.
            live = self._live_feedback()
            return {eid: count_for(live, eid) for eid in _canonical_ids(entry_ids)}

    async def feedback_signals(
        self,
        entry_ids: list[str],
        *,
        count_supersedes: bool = False,
        visibility: Visibility | None = None,
    ) -> dict[str, FeedbackSignals]:
        with self._lock:
            live = self._live_feedback()
            out = {eid: _signals_for(live, eid) for eid in _canonical_ids(entry_ids)}
            if count_supersedes:
                counts: dict[str, int] = {}
                for entry in self._entries.values():
                    if entry.superseded_by in out and self._visible(entry, visibility):
                        counts[entry.superseded_by] = counts.get(entry.superseded_by, 0) + 1
                out = {eid: replace(s, supersedes=counts.get(eid, 0)) for eid, s in out.items()}
            return out

    async def list_feedback(self, entry_id: str, limit: int) -> list[Feedback]:
        if not is_valid_uuid(entry_id):
            return []
        key = str(uuid.UUID(entry_id))
        with self._lock:
            rows = [fb for fb in self._live_feedback().values() if fb.entry_id == key]
        # Informative rows (stale, wrong, or with a note) before bare helpful
        # ones (ADR 0064), each group newest first; ties (same timestamp)
        # break on the reporter, as on Postgres, so the order is deterministic.
        rows.sort(key=lambda fb: (fb.user, fb.agent))
        rows.sort(key=lambda fb: fb.updated_at or EPOCH, reverse=True)
        rows.sort(key=lambda fb: fb.verdict is Verdict.HELPFUL and not fb.note)
        return rows[:limit]

    async def list_feedback_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, list[Feedback]]:
        return {eid: await self.list_feedback(eid, limit) for eid in entry_ids}

    async def list_feedback_log(
        self,
        filters: FeedbackLogFilters,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FeedbackLogItem]:
        def well_formed(value: str | None) -> str | None:
            # ``is_valid_uuid`` first: it refuses the braced and URN spellings
            # ``canonical_uuid`` would accept, as Postgres does here.
            return canonical_uuid(value) if value is not None and is_valid_uuid(value) else None

        entry_id = well_formed(filters.entry_id)
        fleet_id = well_formed(filters.fleet_id)
        if (filters.entry_id is not None and entry_id is None) or (
            filters.fleet_id is not None and fleet_id is None
        ):
            return []
        with self._lock:
            pairs = [
                (fb, entry)
                for fb in self._live_feedback().values()
                if (entry := self._entries.get(fb.entry_id)) is not None
                and (entry_id is None or entry.id == entry_id)
                and (fleet_id is None or entry.fleet_id == fleet_id)
                and (filters.reporter is None or fb.user == filters.reporter)
                and (filters.verdict is None or fb.verdict is filters.verdict)
                and (filters.author is None or entry.author == filters.author)
                and (not filters.authors or entry.author in filters.authors)
                and self._visible(entry, visibility)
            ]
        # Newest first, then entry id, reporter and agent, as on Postgres.
        pairs.sort(key=lambda p: (p[0].entry_id, p[0].user, p[0].agent))
        pairs.sort(key=lambda p: p[0].updated_at or EPOCH, reverse=True)
        return [
            FeedbackLogItem(
                feedback=fb,
                entry_kind=entry.kind,
                entry_summary=entry.summary,
                entry_author=entry.author,
                entry_scope=entry.scope,
                entry_fleet_id=entry.fleet_id,
                entry_state=entry.state,
            )
            for fb, entry in pairs[offset : offset + limit]
        ]

    async def list_flag_backlog(
        self,
        filters: FlagBacklogFilters,
        now: datetime,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FlaggedEntry]:
        fleet_id = filters.fleet_id
        if fleet_id is not None:
            if not is_valid_uuid(fleet_id):  # braced or URN spellings too, as on Postgres
                return []
            fleet_id = canonical_uuid(fleet_id)
        with self._lock:
            revoked = {n for n, a in self._agents.items() if a.status is AgentStatus.REVOKED}
            by_entry: dict[str, list[Feedback]] = {}
            for fb in self._live_feedback().values():
                by_entry.setdefault(fb.entry_id, []).append(fb)
            last_write: dict[str, datetime] = {}
            for e in self._entries.values():
                if e.author not in last_write or e.created_at > last_write[e.author]:
                    last_write[e.author] = e.created_at
            silent_since = now - timedelta(days=ORPHAN_SILENCE_DAYS)
            items: list[FlaggedEntry] = []
            for entry_id, rows in by_entry.items():
                entry = self._entries.get(entry_id)
                flags = [fb.updated_at or now for fb in rows if fb.verdict is not Verdict.HELPFUL]
                if (
                    entry is None
                    or not flags
                    or entry.state is not EntryState.ACTIVE
                    or (fleet_id is not None and entry.fleet_id != fleet_id)
                    or (filters.authors and entry.author not in filters.authors)
                    or not self._visible(entry, visibility)
                ):
                    continue
                verdicts = Counter(fb.verdict for fb in rows)
                orphaned = entry.author in revoked or last_write[entry.author] < silent_since
                first = min(flags)
                if (filters.flagged_before is not None and first >= filters.flagged_before) or (
                    filters.orphaned and not orphaned
                ):
                    continue
                items.append(
                    FlaggedEntry(
                        entry_id=entry.id,
                        kind=entry.kind,
                        summary=entry.summary,
                        author=entry.author,
                        scope=entry.scope,
                        fleet_id=entry.fleet_id,
                        first_flag_at=first,
                        last_flag_at=max(flags),
                        helpful=verdicts[Verdict.HELPFUL],
                        stale=verdicts[Verdict.STALE],
                        wrong=verdicts[Verdict.WRONG],
                        orphaned=orphaned,
                    )
                )
        # Oldest first flag first, then entry id, as on Postgres.
        items.sort(key=lambda i: (i.first_flag_at, i.entry_id))
        return items[offset : offset + limit]

    async def flag_counts(self) -> list[FlagCount]:
        with self._lock:
            now = self._clock()
            flags: dict[str, int] = {}
            confirmed: dict[str, int] = {}
            for fb in self._live_feedback().values():
                if fb.verdict is Verdict.HELPFUL:
                    continue
                entry = self._entries.get(fb.entry_id)
                if entry is None:
                    continue
                at = fb.updated_at or now
                successor = (
                    self._entries.get(entry.superseded_by)
                    if entry.state is EntryState.SUPERSEDED and entry.superseded_by
                    else None
                )
                if successor is not None and successor.created_at <= at:
                    continue  # already superseded when written: predicted nothing
                flags[fb.user] = flags.get(fb.user, 0) + 1
                if successor is not None:
                    confirmed[fb.user] = confirmed.get(fb.user, 0) + 1
            return [FlagCount(u, n, confirmed.get(u, 0)) for u, n in flags.items()]
