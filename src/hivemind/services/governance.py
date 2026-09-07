"""Write and governance services (SPEC.md §4.1, §5, §8).

Thin orchestration over the Store port: embed-then-persist on write,
authorization checks on withdrawal, feedback upsert. No HTTP or MCP
concerns here — the API and MCP layers both consume these services
and both resolve the caller's identity before calling them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from hivemind.domain.entry import Entry, EntryDraft
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential, Embedder, Store
from hivemind.retrieval.scoring import (
    HELPFUL_WEIGHT,
    QUALITY_MAX,
    QUALITY_MIN,
    STALE_WEIGHT,
    WRONG_WEIGHT,
    feedback_quality,
)


class PermissionDenied(Exception):
    """The caller may not perform this governance action."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WriteService:
    """Creates entries: embeds the entry text, persists it, and applies
    any explicit supersessions (SPEC.md §4.1, §7).

    The caller is responsible for having authenticated the request and
    for filling ``draft.author``/``draft.agent`` (the resolved agent
    identity, per SPEC.md §8.1) before calling ``write``.
    """

    def __init__(self, store: Store, embedder: Embedder) -> None:
        self._store = store
        self._embedder = embedder

    async def write(self, draft: EntryDraft) -> Entry:
        """Embed-then-persist a fully-resolved entry draft."""
        embedding = await self._embedder.embed_entry(draft)
        return await self._store.create_entry(draft, embedding)


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """Result of a feedback upsert: the stored row + the entry's new
    quality multiplier (SPEC.md §4.2)."""

    feedback: Feedback
    quality: float


class GovernanceService:
    """Withdrawal and feedback (SPEC.md §4.1, §8.1): the author may
    withdraw their own entries, an admin may withdraw any; feedback is
    open to any authenticated caller who used the entry."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def withdraw(
        self, credential: Credential, entry_id: str, reason: str | None = None
    ) -> Entry:
        entry = await self._store.get_entry(entry_id)
        if entry is None:
            raise LookupError(f"unknown entry: {entry_id}")
        if not (credential.is_admin or entry.author == credential.user_id):
            raise PermissionDenied("only the author or an admin may withdraw an entry")
        if entry.state.value != "active":
            raise ValueError(
                f"entry {entry_id} is already {entry.state.value}; "
                "only active entries can be withdrawn"
            )
        return await self._store.withdraw_entry(entry_id, reason, by_user=credential.user_id)

    async def record_feedback(
        self,
        credential: Credential,
        entry_id: str,
        verdict: Verdict,
        note: str | None = None,
    ) -> FeedbackOutcome:
        """Upsert the caller's verdict for an entry (SPEC.md §4.2).

        One row per (entry, user, agent): the reporter's latest verdict
        wins. The caller must have resolved the agent identity (agent
        sub-key, or the self-reported instance ID for user keys).
        """
        if credential.agent_id is None:
            raise ValueError(
                "the caller's agent identity must be resolved before "
                "recording feedback (SPEC.md §8.1)"
            )
        entry = await self._store.get_entry(entry_id)
        if entry is None:
            raise LookupError(f"unknown entry: {entry_id}")
        feedback = Feedback(
            entry_id=entry_id,
            user=credential.user_id,
            agent=credential.agent_id,
            verdict=verdict,
            note=note,
            updated_at=_utcnow(),
        )
        await self._store.record_feedback(feedback)
        return FeedbackOutcome(
            feedback=feedback,
            quality=await self.quality(entry_id),
        )

    async def quality(self, entry_id: str) -> float:
        """The current quality multiplier for an entry's retrieval score."""
        counts = await self._store.feedback_counts(entry_id)
        return feedback_quality(
            *counts,
            helpful_weight=HELPFUL_WEIGHT,
            stale_weight=STALE_WEIGHT,
            wrong_weight=WRONG_WEIGHT,
            min_quality=QUALITY_MIN,
            max_quality=QUALITY_MAX,
        )
