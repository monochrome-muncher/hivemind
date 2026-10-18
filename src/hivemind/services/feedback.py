"""Feedback on entries (SPEC.md §4.2, §6.4): recording verdicts, the
readable summary (ADR 0051) and the quality multiplier search uses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from hivemind.config import SearchConfig
from hivemind.domain.feedback import (
    FEEDBACK_RECENT_LIMIT,
    NO_SIGNALS,
    Feedback,
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSummary,
    Verdict,
)
from hivemind.domain.validation import (
    MAX_FEEDBACK_IDS,
    MAX_IDENTITY_CHARS,
    MAX_NOTE_CHARS,
    InvalidInput,
    check_text,
)
from hivemind.ports import Credential, Store
from hivemind.retrieval.scoring import feedback_quality
from hivemind.services.chain import get_visible_entries, get_visible_entry
from hivemind.services.errors import AgentIdentityRequired, EntryNotFound, PermissionDenied


def _own_entry_message(ids: list[str]) -> str:
    return (
        f"you cannot rate your own entry ({', '.join(ids)}); "
        "supersede or withdraw it instead (ADR 0065)"
    )


ADMIN_REPORTER = "admin"  # the reserved name ADR 0060 gives the admin key's entries


def reporter_agent(credential: Credential, agent: str | None) -> str | None:
    """The agent a feedback row is filed under (SPEC.md §4.2, ADR 0066).

    The admin key is always ``admin``, whatever it self-reports; an agent
    sub-key's credential agent wins; otherwise the self-reported ``agent``
    (``None`` when there is none, which the caller must refuse).
    """
    if credential.is_admin:
        return ADMIN_REPORTER
    return credential.agent_id or agent


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """Result of a feedback upsert: the stored row + the entry's new
    quality multiplier (SPEC.md §4.2)."""

    feedback: Feedback
    quality: float


def _clean_note(note: str | None) -> str | None:
    """``note`` without surrounding whitespace; ``None`` when nothing is
    left. Stored this way, a whitespace-only note (a no-break space, say)
    can never count as a note: Postgres's ``\\S`` and Python's
    ``str.strip`` disagree on some characters, and the store adapters must
    agree on what "noted" means (SPEC §4.2)."""
    if note is None:
        return None
    return note.strip() or None


class FeedbackService:
    """Feedback is open to any authenticated caller who can read the
    entry (SPEC.md §8.1, §12.2). ``config`` supplies the quality-formula
    weights (SPEC.md §6.4), so the reported quality matches what search
    uses.
    """

    def __init__(
        self,
        store: Store,
        config: SearchConfig | None = None,
        require_stored_keys: bool = False,
    ) -> None:
        self._store = store
        self._config = config or SearchConfig()
        # ADR 0082: a dev-mode credential is downgraded to trust level 0
        # when the deployment requires stored keys.
        self._require_stored_keys = require_stored_keys

    async def record_feedback(
        self,
        credential: Credential,
        entry_id: str,
        verdict: Verdict,
        note: str | None = None,
        agent: str | None = None,
    ) -> FeedbackOutcome:
        """Upsert the caller's verdict for an entry (SPEC.md §4.2).

        One row per (entry, user, agent): the reporter's latest verdict
        wins. The agent identity is ``admin`` for the admin key (ADR 0066),
        the acting sub-key's agent, or the ``agent`` self-reported by a
        plain user key (SPEC.md §8.1).
        """
        check_text(note, "note", MAX_NOTE_CHARS)  # ADR 0040
        note = _clean_note(note)
        check_text(agent, "agent", MAX_IDENTITY_CHARS)
        credential = credential.effective(self._require_stored_keys)
        effective_agent = reporter_agent(credential, agent)
        if effective_agent is None:
            raise AgentIdentityRequired(
                "the caller's agent identity must be resolved before "
                "recording feedback (SPEC.md §8.1)"
            )
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # feedback follows readability (SPEC §12.2, ADR 0033)
            raise EntryNotFound(f"unknown entry: {entry_id}")
        if entry.author == credential.user_id:  # ADR 0065
            raise PermissionDenied(_own_entry_message([entry_id]))
        feedback = Feedback(
            entry_id=entry.id,  # the canonical id, not the caller's spelling
            user=credential.user_id,
            agent=effective_agent,
            verdict=verdict,
            note=note,
        )  # the store stamps ``updated_at`` with its clock (ADR 0079)
        feedback = await self._store.record_feedback(feedback)
        return FeedbackOutcome(
            feedback=feedback,
            quality=await self.quality(entry.id),
        )

    async def record_feedback_many(
        self,
        credential: Credential,
        entry_ids: list[str],
        verdict: Verdict,
        note: str | None = None,
        agent: str | None = None,
    ) -> list[FeedbackOutcome]:
        """One verdict (and note) on several entries at once (ADR 0053).

        All or nothing: if any id is unreadable (``EntryNotFound``) or the
        caller's own entry (``PermissionDenied``, ADR 0065), nothing is
        recorded and the error names them. Spellings of one entry count
        once. A fixed number of store calls however many ids: one read,
        one transactional upsert, one count read.
        """
        check_text(note, "note", MAX_NOTE_CHARS)  # ADR 0040
        note = _clean_note(note)
        check_text(agent, "agent", MAX_IDENTITY_CHARS)
        credential = credential.effective(self._require_stored_keys)
        effective_agent = reporter_agent(credential, agent)
        if effective_agent is None:
            raise AgentIdentityRequired(
                "the caller's agent identity must be resolved before "
                "recording feedback (SPEC.md §8.1)"
            )
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            raise InvalidInput("entry_ids must name at least one entry")
        if len(ids) > MAX_FEEDBACK_IDS:
            raise InvalidInput(f"entry_ids may name at most {MAX_FEEDBACK_IDS} entries")
        found = await get_visible_entries(self._store, ids, credential.visibility())
        missing = [eid for eid in ids if eid not in found]
        if missing:  # feedback follows readability (SPEC §12.2, ADR 0033)
            raise EntryNotFound(f"unknown entries: {', '.join(missing)}")
        own = [eid for eid in ids if found[eid].author == credential.user_id]
        if own:  # ADR 0065: after readability, so an unreadable id stays not_found
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
        rows = await self._store.record_feedback_many(rows)  # stamped (ADR 0079)
        counts = await self._store.quality_counts(canonical)
        kwargs = self._config.quality_kwargs()
        outcomes = []
        for row in rows:
            helpful, stale, wrong = counts.get(row.entry_id, (0, 0, 0))
            quality = feedback_quality(helpful, stale, wrong, **kwargs)
            outcomes.append(FeedbackOutcome(feedback=row, quality=quality))
        return outcomes

    async def feedback_summary(self, entry_id: str) -> FeedbackSummary:
        """What a reader of an entry sees of its feedback (ADR 0051): the
        verdict counts plus the newest rows, notes included.

        No visibility check here: the caller must already have read the
        entry with ``get_visible_entry``.
        """
        signals, recent = await asyncio.gather(
            self._store.feedback_signals([entry_id]),
            self._store.list_feedback(entry_id, FEEDBACK_RECENT_LIMIT),
        )
        s = signals.get(entry_id, NO_SIGNALS)
        return FeedbackSummary(s.helpful, s.stale, s.wrong, tuple(recent), s.last_confirmed_at)

    async def feedback_summaries(self, entry_ids: list[str]) -> dict[str, FeedbackSummary]:
        """``feedback_summary`` for several entries, two store reads in all
        however many there are (ADR 0059). Same caveat: no visibility check."""
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
        """Feedback rows across entries, newest first, each with the entry
        it rates: only rows on entries the caller can read. The admin key
        reads them all; an agent key reads the rows on its own, org and
        home-fleet entries, so the same call can serve an agent's own view."""
        credential = credential.effective(self._require_stored_keys)
        return await self._store.list_feedback_log(
            filters, limit, offset, visibility=credential.visibility()
        )

    async def quality(self, entry_id: str) -> float:
        """An entry's current quality multiplier (SPEC.md §6.4)."""
        helpful, stale, wrong = await self._store.feedback_counts(entry_id)
        return feedback_quality(helpful, stale, wrong, **self._config.quality_kwargs())
