"""Write and governance services (SPEC.md §4.1, §5, §8).

Embed-then-persist on write, authorization on withdrawal, feedback and
pins. Callers (REST and MCP) resolve the caller's identity first.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from hivemind.config import SearchConfig
from hivemind.domain.access import TrustLevel, Visibility, may_supersede
from hivemind.domain.audit import AuditAction
from hivemind.domain.entry import Entry, EntryDraft, EntryState, ExtractedEntity
from hivemind.domain.feedback import FEEDBACK_RECENT_LIMIT, Feedback, FeedbackSummary, Verdict
from hivemind.domain.pin import Pin, PinnedEntry
from hivemind.domain.validation import (
    MAX_FEEDBACK_IDS,
    MAX_ID_CHARS,
    MAX_IDENTITY_CHARS,
    MAX_NOTE_CHARS,
    MAX_PINS_PER_FLEET,
    MAX_REASON_CHARS,
    InvalidInput,
    check_text,
)
from hivemind.ports import Credential, Embedder, Extractor, Store, SupersedeConflict
from hivemind.retrieval.scoring import feedback_quality
from hivemind.services.audit import record_admin_action
from hivemind.services.chain import get_visible_entries, get_visible_entry

logger = logging.getLogger(__name__)


class PermissionDenied(Exception):
    """The caller may not perform this governance action."""


# How far a pin follows supersession to the current version (ADR 0058).
_PIN_FOLLOW_HOPS = 16

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


# How many related entries a write reports (ADR 0052).
RELATED_ON_WRITE_LIMIT = 3


@dataclass(frozen=True, slots=True)
class RelatedEntry:
    """An existing entry near a new one, with their cosine similarity."""

    entry: Entry
    similarity: float


@dataclass(frozen=True, slots=True)
class WriteResult:
    """A stored entry plus the nearest entries its writer can read (ADR 0052)."""

    entry: Entry
    related: tuple[RelatedEntry, ...] = ()


class WriteService:
    """Creates entries: embed, persist, apply supersessions (SPEC.md §4.1,
    §7). An optional ``Extractor`` adds entity facets, best-effort (ADR
    0016). The caller fills ``draft.author``/``draft.agent`` (SPEC.md §8.1).
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
        """Embed-then-persist a fully-resolved entry draft (see ``_write``)."""
        entry, _ = await self._write(draft, writer=writer)
        return entry

    async def write_and_relate(
        self, draft: EntryDraft, *, writer: Visibility | None = None
    ) -> WriteResult:
        """``write``, then the nearest active entries the writer can read
        (ADR 0052), reusing the embedding. Best-effort: the entry is
        already stored, so a failed lookup is logged, not raised.
        """
        entry, embedding = await self._write(draft, writer=writer)
        try:
            related = await self._related(entry.id, embedding, writer)
        except Exception:
            logger.warning("related-entry lookup failed after a write", exc_info=True)
            related = ()
        return WriteResult(entry, related)

    async def _related(
        self, entry_id: str, embedding: list[float], writer: Visibility | None
    ) -> tuple[RelatedEntry, ...]:
        pairs = await self._store.similar_entries(
            embedding, RELATED_ON_WRITE_LIMIT, exclude_id=entry_id, visibility=writer
        )
        if not pairs:
            return ()
        entries = await self._store.get_entries([eid for eid, _ in pairs])
        return tuple(
            RelatedEntry(entries[eid], similarity) for eid, similarity in pairs if eid in entries
        )

    async def _write(
        self, draft: EntryDraft, *, writer: Visibility | None = None
    ) -> tuple[Entry, list[float]]:
        """Embed-then-persist a fully-resolved entry draft; returns the
        stored entry and the embedding it was stored with.

        With ``writer`` (both surfaces pass it), every supersession target
        must pass ``may_supersede`` (ADR 0033) or ``SupersedeDenied`` is
        raised before anything is embedded. ``None`` skips the check
        (internal callers and fixtures only).
        """
        if writer is not None and draft.supersedes:
            await self._check_supersedes(draft, writer)
        if writer is not None and draft.see_also:
            draft = await self._resolve_see_also(draft, writer)
        # Embed and extract concurrently. An embedder failure propagates
        # (cancelling extraction); an extractor failure never fails the write.
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
            entry = await self._store.create_entry(
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
        return entry, embedding

    @staticmethod
    async def _extract(
        draft: EntryDraft, extractor: Extractor
    ) -> tuple[ExtractedEntity, ...] | None:
        """Best-effort extraction (SPEC §13.4): ``None`` on any failure,
        logged since the log is the only record. Cancellation propagates."""
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

    async def _resolve_see_also(self, draft: EntryDraft, writer: Visibility) -> EntryDraft:
        """Every "see also" target must be an entry the writer can read
        (ADR 0057), or the whole write is rejected as invalid input. The
        stored links use the targets' own ids, whatever the spelling."""
        wanted = list(dict.fromkeys(draft.see_also))
        found = await get_visible_entries(self._store, wanted, writer)
        missing = [t for t in wanted if t not in found]
        if missing:
            raise InvalidInput(f"unknown see_also entries: {', '.join(missing)}")
        return replace(draft, see_also=tuple(dict.fromkeys(found[t].id for t in wanted)))

    async def _check_supersedes(self, draft: EntryDraft, writer: Visibility) -> None:
        """Reject the write if any supersession target is out of reach."""
        denied: list[str] = []
        target_ids = list(dict.fromkeys(draft.supersedes))
        # Batched lookups (SP-13), chunked to bound memory.
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

    ``config`` supplies the quality-formula weights (SPEC.md §6.4), so
    the reported quality matches what search uses.
    """

    def __init__(self, store: Store, config: SearchConfig | None = None) -> None:
        self._store = store
        self._config = config or SearchConfig()

    async def withdraw(
        self, credential: Credential, entry_id: str, reason: str | None = None
    ) -> Entry:
        check_text(reason, "reason", MAX_REASON_CHARS)  # ADR 0040
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
            # An admin withdrawing someone else's entry is audited (ADR 0027).
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
        check_text(note, "note", MAX_NOTE_CHARS)  # ADR 0040
        check_text(agent, "agent", MAX_IDENTITY_CHARS)
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
            raise LookupError(f"unknown entries: {', '.join(missing)}")
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

    # -- pinned entries (ADR 0058) -------------------------------------------

    async def pin(self, credential: Credential, entry_id: str) -> Pin:
        """Pin an active fleet entry to its own fleet. A privileged agent of
        that fleet or an admin may; a fleet holds at most
        ``MAX_PINS_PER_FLEET`` pins. Pinning a pinned entry is a no-op."""
        entry, fleet_id = await self._pinnable(credential, entry_id)
        if entry.state is not EntryState.ACTIVE:
            raise ValueError(
                f"entry {entry_id} is {entry.state.value}; only active entries can be pinned"
            )
        pin = await self._store.pin_entry(
            fleet_id,
            entry.id,
            credential.agent_name or credential.user_id,
            MAX_PINS_PER_FLEET,
        )
        if pin is None:
            raise InvalidInput(
                f"the fleet already has {MAX_PINS_PER_FLEET} pinned entries: unpin one first"
            )
        return pin

    async def unpin(self, credential: Credential, entry_id: str) -> bool:
        """Remove an entry's pin (any state); whether it was pinned."""
        entry, fleet_id = await self._pinnable(credential, entry_id)
        return await self._store.unpin_entry(fleet_id, entry.id)

    async def pinned(
        self, credential: Credential, fleet_id: str | None = None
    ) -> list[PinnedEntry]:
        """A fleet's pinned entries as ``credential`` sees them, newest pin
        first (the caller's home fleet unless ``fleet_id`` names another).

        A pin follows supersession to the current version; a withdrawn
        entry shows as withdrawn until unpinned. Unreadable entries are
        left out.
        """
        fleet = fleet_id or credential.home_fleet_id
        if fleet is None:
            return []
        check_text(fleet, "fleet_id", MAX_ID_CHARS)
        pins = await self._store.list_pins(fleet)
        if not pins:
            return []
        visibility = credential.visibility()
        found = await get_visible_entries(self._store, [p.entry_id for p in pins], visibility)
        result: list[PinnedEntry] = []
        for pin in pins:
            entry = found.get(pin.entry_id)
            if entry is None:
                continue
            result.append(PinnedEntry(pin, await self._current(entry, visibility)))
        return result

    async def _current(self, entry: Entry, visibility: Visibility) -> Entry:
        """The newest version of ``entry`` the reader may see (bounded walk)."""
        for _ in range(_PIN_FOLLOW_HOPS):
            if entry.state is not EntryState.SUPERSEDED or entry.superseded_by is None:
                break
            successor = await get_visible_entry(self._store, entry.superseded_by, visibility)
            if successor is None:
                break
            entry = successor
        return entry

    async def _pinnable(self, credential: Credential, entry_id: str) -> tuple[Entry, str]:
        """The entry to pin or unpin and its fleet, if ``credential`` may
        (ADR 0058)."""
        entry = await get_visible_entry(self._store, entry_id, credential.visibility())
        if entry is None:  # unknown, or not visible to the caller (ADR 0033)
            raise LookupError(f"unknown entry: {entry_id}")
        if entry.scope != "fleet" or entry.fleet_id is None:
            raise InvalidInput("only fleet entries can be pinned, to their own fleet")
        privileged_member = (
            credential.trust_level is TrustLevel.PRIVILEGED
            and credential.home_fleet_id == entry.fleet_id
        )
        if not (credential.is_admin or privileged_member):
            raise PermissionDenied(
                "only a privileged agent of the entry's fleet or an admin may pin or unpin"
            )
        return entry, entry.fleet_id
