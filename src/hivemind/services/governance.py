"""Write and governance services (SPEC.md §4.1, §5, §8).

Thin orchestration over the Store port: embed-then-persist on write,
authorization checks on withdrawal, feedback upsert. No HTTP or MCP
concerns here — the API and MCP layers both consume these services
and both resolve the caller's identity before calling them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from hivemind.config import SearchConfig
from hivemind.domain.entry import Entry, EntryDraft, ExtractedEntity
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential, Embedder, Extractor, Store
from hivemind.retrieval.scoring import feedback_quality

logger = logging.getLogger(__name__)


class PermissionDenied(Exception):
    """The caller may not perform this governance action."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


class WriteService:
    """Creates entries: embeds the entry text, persists it, and applies
    any explicit supersessions (SPEC.md §4.1, §7).

    Entity extraction is an optional, best-effort enrichment (ADR 0016,
    SPEC §13): pass an ``Extractor`` to extract entity facets at write
    time; an extraction failure never blocks the write — the entry
    lands without facets. Absent (``None``), extraction is off, zero
    LLM cost (the same dev-mode stance as "no authenticator").

    The caller is responsible for having authenticated the request and
    for filling ``draft.author``/``draft.agent`` (the resolved agent
    identity, per SPEC.md §8.1) before calling ``write``.
    """

    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        extractor: Extractor | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._extractor = extractor

    async def write(self, draft: EntryDraft) -> Entry:
        """Embed-then-persist a fully-resolved entry draft.

        The embedding model name is recorded per entry (SPEC.md §7) so
        the vector's provenance is traceable. When an extractor is set,
        entity facets are extracted over the same text and recorded on
        the entry (``entities`` / ``entities_model``, ADR 0016) —
        best-effort: an extraction failure never blocks the write, the
        entry simply lands without facets.
        """
        embedding = await self._embedder.embed_entry(draft)
        entities: tuple[ExtractedEntity, ...] = ()
        entities_model: str | None = None
        if self._extractor is not None:
            try:
                entities = await self._extractor.extract_entry(draft)
                entities_model = self._extractor.model_name
            except Exception:
                # Best-effort enrichment (ADR 0016, SPEC §13.4): an
                # extraction failure never blocks the write — the entry
                # lands without facets, zero write-failure impact. This
                # is the ONLY record of what failed — the extractor's own
                # exception is logged here, not just swallowed, so a
                # dead/misconfigured extractor endpoint shows up as log
                # lines instead of only as a slow drift in facet coverage
                # (the symptom docs/ops-runbook.md previously said to
                # watch for in place of an actual error).
                logger.warning(
                    "entity extraction failed for a write by %s/%s — entry lands without facets",
                    draft.author,
                    draft.agent,
                    exc_info=True,
                )
                entities, entities_model = (), None
        return await self._store.create_entry(
            draft,
            embedding,
            embedding_model=self._embedder.model_name,
            entities=entities,
            entities_model=entities_model,
        )


@dataclass(frozen=True, slots=True)
class FeedbackOutcome:
    """Result of a feedback upsert: the stored row + the entry's new
    quality multiplier (SPEC.md §4.2)."""

    feedback: Feedback
    quality: float


class GovernanceService:
    """Withdrawal and feedback (SPEC.md §4.1, §8.1): the author may
    withdraw their own entries, an admin may withdraw any; feedback is
    open to any authenticated caller who used the entry.

    ``config`` supplies the quality-formula weights (SPEC.md §6.4:
    “the formula is a config value, not a constant in code”); a default
    ``SearchConfig`` (the v1 defaults) is used when omitted, so the
    service's reported quality always matches what search uses.
    """

    def __init__(self, store: Store, config: SearchConfig | None = None) -> None:
        self._store = store
        self._config = config or SearchConfig()

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
        agent: str | None = None,
    ) -> FeedbackOutcome:
        """Upsert the caller's verdict for an entry (SPEC.md §4.2).

        One row per (entry, user, agent): the reporter's latest verdict
        wins. The agent identity is the acting sub-key's agent, or the
        ``agent`` self-reported by a plain user key (SPEC.md §8.1).
        """
        effective_agent = credential.agent_id or agent
        if effective_agent is None:
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

    async def quality(self, entry_id: str) -> float:
        """The current quality multiplier for an entry's retrieval score.

        Uses the configured weights (SPEC.md §6.4) so the multiplier
        reported here always matches the one search rescoring applies.
        """
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
