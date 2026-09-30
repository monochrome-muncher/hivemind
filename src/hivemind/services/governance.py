"""Write and governance services (SPEC.md §4.1, §5, §8).

Thin orchestration over the Store port: embed-then-persist on write,
authorization checks on withdrawal, feedback upsert. No HTTP or MCP
concerns here — the API and MCP layers both consume these services
and both resolve the caller's identity before calling them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from hivemind.config import SearchConfig
from hivemind.domain.access import Visibility, may_supersede
from hivemind.domain.audit import AuditAction
from hivemind.domain.entry import Entry, EntryDraft, ExtractedEntity
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential, Embedder, Extractor, Store, SupersedeConflict
from hivemind.retrieval.scoring import feedback_quality
from hivemind.services.audit import record_admin_action
from hivemind.services.chain import get_visible_entry

logger = logging.getLogger(__name__)


class PermissionDenied(Exception):
    """The caller may not perform this governance action."""


# ``supersedes`` targets fetched per ``get_entries`` call (bounds memory).
_SUPERSEDES_LOOKUP_CHUNK = 50


class SupersedeDenied(PermissionDenied):
    """A write named supersession targets outside the writer's reach
    (ADR 0033) or at a target that is no longer active (ADR 0034).
    ``ids`` are those targets; the message deliberately says
    "not found or not supersedable by you" for all cases."""

    def __init__(self, ids: list[str]) -> None:
        super().__init__(
            "not found or not supersedable by you: "
            + ", ".join(ids)
            + " (you may supersede active entries you can read, with a successor that "
            "reaches at least the same audience — ADR 0033; only the current head of a "
            "chain is supersedable — ADR 0034)"
        )
        self.ids = ids


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

    async def write(self, draft: EntryDraft, *, writer: Visibility | None = None) -> Entry:
        """Embed-then-persist a fully-resolved entry draft.

        ``writer`` is the caller's visibility. When given, every
        ``draft.supersedes`` target must pass ``may_supersede`` (ADR 0033)
        or the whole write is rejected with ``SupersedeDenied`` before
        anything is embedded or stored. Both surfaces pass it; ``None``
        skips the check (internal callers and fixtures only).

        The embedding model name is recorded per entry (SPEC.md §7) so
        the vector's provenance is traceable. When an extractor is set,
        entity facets are extracted over the same text and recorded on
        the entry (``entities`` / ``entities_model``, ADR 0016) —
        best-effort: an extraction failure never blocks the write, the
        entry simply lands without facets.
        """
        if writer is not None and draft.supersedes:
            await self._check_supersedes(draft, writer)
        # Embedder and (best-effort) extractor are independent network
        # calls: run them concurrently. The embedder's failure propagates
        # unchanged (the extractor is cancelled); the extractor's never
        # fails the write. Cancelling the write cancels both.
        extraction: asyncio.Task[tuple[ExtractedEntity, ...] | None] | None = None
        entities: tuple[ExtractedEntity, ...] = ()
        entities_model: str | None = None
        if self._extractor is not None:
            extraction = asyncio.create_task(self._extract(draft, self._extractor))
        try:
            embedding = await self._embedder.embed_entry(draft)
            if extraction is not None and (extracted := await extraction) is not None:
                entities = extracted
                entities_model = self._extractor.model_name if self._extractor else None
        except BaseException:
            if extraction is not None:
                extraction.cancel()
            raise
        try:
            return await self._store.create_entry(
                draft,
                embedding,
                embedding_model=self._embedder.model_name,
                entities=entities,
                entities_model=entities_model,
            )
        except SupersedeConflict as exc:
            # A target was superseded/withdrawn after the pre-check
            # (ADR 0034): the store rolled the write back atomically.
            raise SupersedeDenied(exc.ids) from exc

    @staticmethod
    async def _extract(
        draft: EntryDraft, extractor: Extractor
    ) -> tuple[ExtractedEntity, ...] | None:
        """Best-effort extraction (ADR 0016, SPEC §13.4): ``None`` on any
        failure, which is logged — the only record of what failed — so a
        dead extractor endpoint shows up as log lines. Cancellation is not
        swallowed (``CancelledError`` is a ``BaseException``)."""
        try:
            return await extractor.extract_entry(draft)
        except Exception:
            logger.warning(
                "entity extraction failed for a write by %s/%s — entry lands without facets",
                draft.author,
                draft.agent,
                exc_info=True,
            )
            return None

    async def _check_supersedes(self, draft: EntryDraft, writer: Visibility) -> None:
        """Reject the write if any supersession target is out of reach."""
        denied: list[str] = []
        target_ids = list(dict.fromkeys(draft.supersedes))
        # Batched lookups (SP-13), chunked so memory stays bounded however
        # long the list is: each chunk is checked and dropped before the next.
        for i in range(0, len(target_ids), _SUPERSEDES_LOOKUP_CHUNK):
            chunk = target_ids[i : i + _SUPERSEDES_LOOKUP_CHUNK]
            targets = await self._store.get_entries(chunk)
            for target_id in chunk:
                target = targets.get(target_id)
                if target is None or not may_supersede(
                    target, new_scope=draft.scope, new_fleet_id=draft.fleet_id, writer=writer
                ):
                    denied.append(target_id)
        if denied:
            raise SupersedeDenied(denied)


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
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # unknown, or not visible to the caller (ADR 0033)
            raise LookupError(f"unknown entry: {entry_id}")
        if not (credential.is_admin or entry.author == credential.user_id):
            raise PermissionDenied("only the author or an admin may withdraw an entry")
        if entry.state.value != "active":
            raise ValueError(
                f"entry {entry_id} is already {entry.state.value}; "
                "only active entries can be withdrawn"
            )
        withdrawn = await self._store.withdraw_entry(entry_id, reason, by_user=credential.user_id)
        if credential.is_admin and entry.author != credential.user_id:
            # Authorised by admin privilege, not authorship (SPEC §4.1):
            # an admin action on someone else's entry — audited (ADR
            # 0027). An author withdrawing their own entry is not.
            await record_admin_action(
                self._store,
                credential,
                AuditAction.ENTRY_WITHDRAW,
                entry_id,
                {"author": entry.author, "reason": reason},
            )
        return withdrawn

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
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # feedback follows readability (SPEC §12.2, ADR 0033)
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
