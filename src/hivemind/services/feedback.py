"""Feedback on entries (SPEC.md §4.2, §6.4): recording verdicts, the
readable summary (ADR 0051) and the quality multiplier search uses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from hivemind.config import SearchConfig
from hivemind.domain.feedback import FEEDBACK_RECENT_LIMIT, Feedback, FeedbackSummary, Verdict
from hivemind.domain.validation import (
    MAX_FEEDBACK_IDS,
    MAX_IDENTITY_CHARS,
    MAX_NOTE_CHARS,
    InvalidInput,
    check_text,
)
from hivemind.ports import Credential, Store
from hivemind.retrieval.scoring import feedback_quality
from hivemind.services.chain import get_visible_entry
from hivemind.services.errors import AgentIdentityRequired, EntryNotFound


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """Result of a feedback upsert: the stored row + the entry's new
    quality multiplier (SPEC.md §4.2)."""

    feedback: Feedback
    quality: float


class FeedbackService:
    """Feedback is open to any authenticated caller who can read the
    entry (SPEC.md §8.1, §12.2). ``config`` supplies the quality-formula
    weights (SPEC.md §6.4), so the reported quality matches what search
    uses.
    """

    def __init__(self, store: Store, config: SearchConfig | None = None) -> None:
        self._store = store
        self._config = config or SearchConfig()

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
        wins. The agent identity is the acting sub-key's agent, or the
        ``agent`` self-reported by a plain user key (SPEC.md §8.1).
        """
        check_text(note, "note", MAX_NOTE_CHARS)  # ADR 0040
        check_text(agent, "agent", MAX_IDENTITY_CHARS)
        effective_agent = credential.agent_id or agent
        if effective_agent is None:
            raise AgentIdentityRequired(
                "the caller's agent identity must be resolved before "
                "recording feedback (SPEC.md §8.1)"
            )
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # feedback follows readability (SPEC §12.2, ADR 0033)
            raise EntryNotFound(f"unknown entry: {entry_id}")
        feedback = Feedback(
            entry_id=entry_id,
            user=credential.user_id,
            agent=effective_agent,
            verdict=verdict,
            note=note,
            updated_at=_utcnow(),
        )
        await self._store.record_feedback(feedback)
        return FeedbackOutcome(
            feedback=feedback,
            quality=await self.quality(entry_id),
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

        All or nothing: if any id is unreadable, nothing is recorded and
        ``LookupError`` names them. Repeated ids count once.
        """
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            raise InvalidInput("entry_ids must name at least one entry")
        if len(ids) > MAX_FEEDBACK_IDS:
            raise InvalidInput(f"entry_ids may name at most {MAX_FEEDBACK_IDS} entries")
        visibility = credential.visibility()
        missing = [
            eid for eid in ids if await get_visible_entry(self._store, eid, visibility) is None
        ]
        if missing:  # feedback follows readability (SPEC §12.2, ADR 0033)
            raise EntryNotFound(f"unknown entries: {', '.join(missing)}")
        return [
            await self.record_feedback(credential, eid, verdict, note, agent=agent) for eid in ids
        ]

    async def feedback_summary(self, entry_id: str) -> FeedbackSummary:
        """What a reader of an entry sees of its feedback (ADR 0051): the
        verdict counts plus the newest rows, notes included.

        No visibility check here: the caller must already have read the
        entry with ``get_visible_entry``.
        """
        (helpful, stale, wrong), recent = await asyncio.gather(
            self._store.feedback_counts(entry_id),
            self._store.list_feedback(entry_id, FEEDBACK_RECENT_LIMIT),
        )
        return FeedbackSummary(helpful, stale, wrong, tuple(recent))

    async def feedback_summaries(self, entry_ids: list[str]) -> dict[str, FeedbackSummary]:
        """``feedback_summary`` for several entries, two store reads in all
        however many there are (ADR 0059). Same caveat: no visibility check."""
        counts, recent = await asyncio.gather(
            self._store.quality_counts(entry_ids),
            self._store.list_feedback_many(entry_ids, FEEDBACK_RECENT_LIMIT),
        )
        return {
            eid: FeedbackSummary(*counts.get(eid, (0, 0, 0)), tuple(recent.get(eid, ())))
            for eid in entry_ids
        }

    async def quality(self, entry_id: str) -> float:
        """An entry's current quality multiplier (SPEC.md §6.4)."""
        helpful, stale, wrong = await self._store.feedback_counts(entry_id)
        cfg = self._config
        return feedback_quality(
            helpful,
            stale,
            wrong,
            helpful_weight=cfg.quality_helpful_weight,
            stale_weight=cfg.quality_stale_weight,
            wrong_weight=cfg.quality_wrong_weight,
            min_quality=cfg.quality_min,
            max_quality=cfg.quality_max,
        )
