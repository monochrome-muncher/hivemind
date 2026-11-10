"""Feedback on entries (SPEC.md §4.2, §6.4): recording verdicts, the
readable summary (ADR 0051) and the quality multiplier search uses.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from hivemind.config import SearchConfig
from hivemind.domain.access import may_supersede
from hivemind.domain.entry import Entry
from hivemind.domain.feedback import (
    FEEDBACK_RECENT_LIMIT,
    NO_SIGNALS,
    Feedback,
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSignals,
    FeedbackSummary,
    FlagBacklogFilters,
    FlaggedEntry,
    Verdict,
)
from hivemind.domain.validation import (
    MAX_FEEDBACK_IDS,
    MAX_IDENTITY_CHARS,
    MAX_NOTE_CHARS,
    InvalidInput,
    check_text,
    utcnow,
)
from hivemind.ports import Credential, Store
from hivemind.retrieval.scoring import feedback_quality
from hivemind.services.access import resolve_write_scope
from hivemind.services.chain import get_visible_entries, get_visible_entry
from hivemind.services.errors import AgentIdentityRequired, EntryNotFound, PermissionDenied
from hivemind.services.ownership import own_author_filter


def _own_entry_message(ids: list[str]) -> str:
    return (
        f"you cannot rate your own entry ({', '.join(ids)}); "
        "supersede or withdraw it instead (ADR 0065)"
    )


ADMIN_REPORTER = "admin"  # reserved name (ADR 0060)


def reporter_agent(credential: Credential, agent: str | None) -> str | None:
    """The agent a feedback row is filed under (ADR 0066): ``admin`` for the admin key, else the
    credential's agent, else the self-reported one (``None``: the caller must refuse)."""
    if credential.is_admin:
        return ADMIN_REPORTER
    return credential.agent_id or agent


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """A feedback upsert's stored row and the entry's new quality multiplier (SPEC.md §4.2)."""

    feedback: Feedback
    quality: float
    # True only for a ``stale`` or ``wrong`` verdict the caller could fix at its default scope (ADR
    # 0128).
    may_supersede: bool = False


def _clean_note(note: str | None) -> str | None:
    """``note`` stripped, ``None`` when empty. Postgres's ``\\S`` and ``str.strip`` disagree on some
    characters (a no-break space), and the adapters must agree on what "noted" means (SPEC §4.2)."""
    if note is None:
        return None
    return note.strip() or None


def _supersede_check(credential: Credential, verdict: Verdict) -> Callable[[Entry], bool]:
    """Whether the reporter could fix an entry it flags (ADR 0128): a ``stale`` or ``wrong``
    verdict, an access-controlled agent key (not admin, ADR 0060; not legacy, which would widen
    to ``org``) and a default-scope write that would supersede it."""
    if verdict is Verdict.HELPFUL or credential.is_admin or not credential.access_controlled:
        return lambda _entry: False
    try:
        target = resolve_write_scope(credential)
    except PermissionDenied:
        return lambda _entry: False
    visibility = credential.visibility()
    return lambda entry: may_supersede(
        entry, new_scope=target.scope, new_fleet_id=target.fleet_id, writer=visibility
    )


class FeedbackService:
    """Feedback, open to any caller who can read the entry (SPEC.md §8.1, §12.2). ``config``
    gives the quality weights (SPEC.md §6.4) so the quality matches search."""

    def __init__(
        self,
        store: Store,
        config: SearchConfig | None = None,
        require_stored_keys: bool = False,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._config = config or SearchConfig()
        self._now_fn: Callable[[], datetime] = now_fn or utcnow
        # A dev-mode credential drops to trust level 0 when stored keys are required (ADR 0082).
        self._require_stored_keys = require_stored_keys

    def _reporter(
        self, credential: Credential, note: str | None, agent: str | None
    ) -> tuple[Credential, str | None, str]:
        """Validate, then resolve the effective credential, cleaned note and agent identity."""
        check_text(note, "note", MAX_NOTE_CHARS)
        note = _clean_note(note)
        check_text(agent, "agent", MAX_IDENTITY_CHARS)
        credential = credential.effective(self._require_stored_keys)
        effective_agent = reporter_agent(credential, agent)
        if effective_agent is None:
            raise AgentIdentityRequired(
                "the caller's agent identity must be resolved before "
                "recording feedback (SPEC.md §8.1)"
            )
        return credential, note, effective_agent

    async def record_feedback(
        self,
        credential: Credential,
        entry_id: str,
        verdict: Verdict,
        note: str | None = None,
        agent: str | None = None,
    ) -> FeedbackOutcome:
        """Upsert the caller's verdict (SPEC.md §4.2): one row per (entry, user, agent), the
        latest wins."""
        credential, note, effective_agent = self._reporter(credential, note, agent)
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # feedback follows readability (SPEC §12.2)
            raise EntryNotFound(f"unknown entry: {entry_id}")
        if credential.owns(entry.author):  # ADR 0065
            raise PermissionDenied(_own_entry_message([entry_id]))
        feedback = Feedback(
            entry_id=entry.id,  # the canonical id, not the caller's spelling
            user=credential.user_id,
            agent=effective_agent,
            verdict=verdict,
            note=note,
        )  # the store stamps ``updated_at`` (ADR 0079)
        feedback = await self._store.record_feedback(feedback)
        return FeedbackOutcome(
            feedback=feedback,
            quality=await self.quality(entry.id),
            may_supersede=_supersede_check(credential, verdict)(entry),
        )

    async def record_feedback_many(
        self,
        credential: Credential,
        entry_ids: list[str],
        verdict: Verdict,
        note: str | None = None,
        agent: str | None = None,
    ) -> list[FeedbackOutcome]:
        """One verdict on several entries at once (ADR 0053), all or nothing: an unreadable or own
        entry (ADR 0065) records nothing and the error names them. A fixed number of store calls."""
        credential, note, effective_agent = self._reporter(credential, note, agent)
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            raise InvalidInput("entry_ids must name at least one entry")
        if len(ids) > MAX_FEEDBACK_IDS:
            raise InvalidInput(f"entry_ids may name at most {MAX_FEEDBACK_IDS} entries")
        found = await get_visible_entries(self._store, ids, credential.visibility())
        missing = [eid for eid in ids if eid not in found]
        if missing:  # feedback follows readability (SPEC §12.2)
            raise EntryNotFound(f"unknown entries: {', '.join(missing)}")
        own = [eid for eid in ids if credential.owns(found[eid].author)]
        if own:  # after readability, so an unreadable id stays not_found
            raise PermissionDenied(_own_entry_message(own))
        canonical = list(dict.fromkeys(found[eid].id for eid in ids))
        rows = [
            Feedback(
                entry_id=eid,
                user=credential.user_id,
                agent=effective_agent,
                verdict=verdict,
                note=note,
            )
            for eid in canonical
        ]
        rows = await self._store.record_feedback_many(rows)
        signals = await self._store.feedback_signals(canonical)
        fixable = _supersede_check(credential, verdict)
        by_id = {e.id: e for e in found.values()}
        return [
            FeedbackOutcome(
                feedback=row,
                quality=self._quality(signals.get(row.entry_id, NO_SIGNALS)),
                may_supersede=fixable(by_id[row.entry_id]),
            )
            for row in rows
        ]

    async def feedback_summary(self, entry_id: str) -> FeedbackSummary:
        """Verdict counts plus the newest rows, notes included (ADR 0051). No visibility check: the
        caller must already have read the entry with ``get_visible_entry``."""
        signals, recent = await asyncio.gather(
            self._store.feedback_signals([entry_id]),
            self._store.list_feedback(entry_id, FEEDBACK_RECENT_LIMIT),
        )
        s = signals.get(entry_id, NO_SIGNALS)
        return FeedbackSummary(s.helpful, s.stale, s.wrong, tuple(recent), s.last_confirmed_at)

    async def feedback_summaries(self, entry_ids: list[str]) -> dict[str, FeedbackSummary]:
        """``feedback_summary`` for several entries, two store reads (ADR 0059); no visibility
        check."""
        signals, recent = await asyncio.gather(
            self._store.feedback_signals(entry_ids),
            self._store.list_feedback_many(entry_ids, FEEDBACK_RECENT_LIMIT),
        )
        return {
            eid: FeedbackSummary(
                *(s := signals.get(eid, NO_SIGNALS)).counts,
                tuple(recent.get(eid, ())),
                s.last_confirmed_at,
            )
            for eid in entry_ids
        }

    async def feedback_log(
        self,
        credential: Credential,
        filters: FeedbackLogFilters,
        *,
        limit: int,
        offset: int = 0,
    ) -> list[FeedbackLogItem]:
        """Feedback rows across entries, newest first, only on entries the caller can read (admin:
        all; an agent: its own, org and home-fleet entries)."""
        credential = credential.effective(self._require_stored_keys)
        visibility = credential.visibility()
        return await self._store.list_feedback_log(
            own_author_filter(filters, visibility), limit, offset, visibility=visibility
        )

    async def flag_backlog(
        self,
        credential: Credential,
        *,
        fleet_id: str | None = None,
        older_than: timedelta | None = None,
        orphaned: bool = False,
        own: bool = False,
        limit: int,
        offset: int = 0,
    ) -> list[FlaggedEntry]:
        """Active entries with a standing ``stale`` or ``wrong`` verdict, oldest flag first
        (ADR 0093), that the caller can read. ``older_than`` keeps entries first flagged longer ago;
        ``own`` keeps the caller's and inherited ones (ADR 0115)."""
        credential = credential.effective(self._require_stored_keys)
        now = self._now_fn()
        visibility = credential.visibility()
        filters = FlagBacklogFilters(
            fleet_id=fleet_id,
            flagged_before=now - older_than if older_than else None,
            orphaned=orphaned,
            authors=visibility.owned if own else (),
        )
        return await self._store.list_flag_backlog(
            filters, now, limit, offset, visibility=visibility
        )

    async def quality(self, entry_id: str) -> float:
        """An entry's current quality multiplier."""
        signals = await self._store.feedback_signals([entry_id])
        return self._quality(signals.get(entry_id, NO_SIGNALS))

    def _quality(self, signals: FeedbackSignals) -> float:
        kwargs = self._config.quality_kwargs(confirmed=signals.last_confirmed_at is not None)
        return feedback_quality(signals.helpful, signals.stale, signals.wrong, **kwargs)
