"""The write path (SPEC.md §4.1, §7): embed, extract, check supersessions
and see-also links, persist, and report related entries (ADR 0052).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

from hivemind.domain.access import Visibility, may_supersede
from hivemind.domain.entry import Entry, EntryDraft, ExtractedEntity
from hivemind.domain.feedback import FeedbackCounts
from hivemind.domain.validation import InvalidInput
from hivemind.ports import Embedder, Extractor, Store, SupersedeConflict
from hivemind.services.chain import get_visible_entries
from hivemind.services.errors import SupersedeDenied
from hivemind.services.write_warnings import (
    WriteWarning,
    near_duplicate_warning,
    write_warnings,
)

# The name these warnings were always logged under, so log filters keep matching.
logger = logging.getLogger("hivemind.services.governance")


# ``supersedes`` targets fetched per ``get_entries`` call (bounds memory).
_SUPERSEDES_LOOKUP_CHUNK = 50


# How many related entries a write reports (ADR 0052).
RELATED_ON_WRITE_LIMIT = 3


def _id_key(entry_id: str) -> str:
    """An entry id with case and hyphens folded away (uuid spellings)."""
    return entry_id.replace("-", "").lower()


@dataclass(frozen=True, slots=True)
class RelatedEntry:
    """An existing entry near a new one, with their cosine similarity, its
    feedback counts and whether the writer also wrote it."""

    entry: Entry
    similarity: float
    feedback: FeedbackCounts = (0, 0, 0)
    same_author: bool = False


@dataclass(frozen=True, slots=True)
class WriteResult:
    """A stored entry plus the nearest entries its writer can read (ADR 0052)."""

    entry: Entry
    related: tuple[RelatedEntry, ...] = ()
    warnings: tuple[WriteWarning, ...] = ()


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What a write would report, with nothing stored (check mode)."""

    related: tuple[RelatedEntry, ...] = ()
    warnings: tuple[WriteWarning, ...] = ()


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
        entry, _, _ = await self._write(draft, writer=writer)
        return entry

    async def write_and_relate(
        self, draft: EntryDraft, *, writer: Visibility | None = None
    ) -> WriteResult:
        """``write``, then the nearest active entries the writer can read
        (ADR 0052), reusing the embedding. Best-effort: the entry is
        already stored, so a failed lookup is logged, not raised.
        """
        entry, embedding, warnings = await self._write(draft, writer=writer)
        related = await self._related_best_effort(draft, entry.id, embedding, writer)
        return WriteResult(entry, related, warnings + near_duplicate_warning(related))

    async def check(self, draft: EntryDraft, *, writer: Visibility | None = None) -> CheckResult:
        """Check mode: everything ``write_and_relate`` validates (supersede
        permission, see-also resolution) and reports (``related``, warnings),
        but nothing is stored and the extractor does not run. Raises what a
        real write would raise before it stores. The entries the draft
        supersedes are left out of ``related``, as a real write leaves them
        out once they are superseded.
        """
        draft, warnings, superseded = await self._prepare(draft, writer)
        embedding = await self._embedder.embed_entry(draft)
        related = await self._related_best_effort(
            draft, None, embedding, writer, skip=frozenset(e.id for e in superseded)
        )
        return CheckResult(related, warnings + near_duplicate_warning(related))

    async def _related_best_effort(
        self,
        draft: EntryDraft,
        entry_id: str | None,
        embedding: list[float],
        writer: Visibility | None,
        skip: frozenset[str] = frozenset(),
    ) -> tuple[RelatedEntry, ...]:
        try:
            return await self._related(draft, entry_id, embedding, writer, skip)
        except Exception:
            logger.warning("related-entry lookup failed", exc_info=True)
            return ()

    async def _related(
        self,
        draft: EntryDraft,
        entry_id: str | None,
        embedding: list[float],
        writer: Visibility | None,
        skip: frozenset[str] = frozenset(),
    ) -> tuple[RelatedEntry, ...]:
        # ``skip`` (ids the caller will not want back) widens the lookup by
        # its size, so the list still holds up to RELATED_ON_WRITE_LIMIT.
        pairs = await self._store.similar_entries(
            embedding, RELATED_ON_WRITE_LIMIT + len(skip), exclude_id=entry_id, visibility=writer
        )
        pairs = [(eid, sim) for eid, sim in pairs if eid not in skip][:RELATED_ON_WRITE_LIMIT]
        if not pairs:
            return ()
        ids = [eid for eid, _ in pairs]
        entries = await self._store.get_entries(ids)
        try:
            counts = await self._store.quality_counts(ids)  # one batched read
        except Exception:
            # Counts are garnish: keep the related list without them.
            logger.warning("feedback counts failed for related entries", exc_info=True)
            counts = {}
        return tuple(
            RelatedEntry(
                entries[eid],
                similarity,
                feedback=counts.get(eid, (0, 0, 0)),
                same_author=entries[eid].author == draft.author,
            )
            for eid, similarity in pairs
            if eid in entries
        )

    async def _prepare(
        self, draft: EntryDraft, writer: Visibility | None
    ) -> tuple[EntryDraft, tuple[WriteWarning, ...], list[Entry]]:
        """The checks a write makes before it embeds (supersede permission,
        see-also resolution), the warnings the loaded targets allow, and the
        supersession targets."""
        superseded: list[Entry] = []
        linked: list[Entry] = []
        if writer is not None and draft.supersedes:
            superseded = await self._check_supersedes(draft, writer)
        if writer is not None and draft.see_also:
            draft, linked = await self._resolve_see_also(draft, writer)
        return draft, write_warnings(draft, superseded, linked), superseded

    async def _write(
        self, draft: EntryDraft, *, writer: Visibility | None = None
    ) -> tuple[Entry, list[float], tuple[WriteWarning, ...]]:
        """Embed-then-persist a fully-resolved entry draft; returns the
        stored entry, the embedding it was stored with and the warnings.

        With ``writer`` (both surfaces pass it), every supersession target
        must pass ``may_supersede`` (ADR 0033) or ``SupersedeDenied`` is
        raised before anything is embedded. ``None`` skips the check
        (internal callers and fixtures only).
        """
        draft, warnings, _ = await self._prepare(draft, writer)
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
        return entry, embedding, warnings

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

    async def _resolve_see_also(
        self, draft: EntryDraft, writer: Visibility
    ) -> tuple[EntryDraft, list[Entry]]:
        """Every "see also" target must be an entry the writer can read
        (ADR 0057), or the whole write is rejected as invalid input. The
        stored links use the targets' own ids, whatever the spelling. Also
        returns the targets, for the warnings."""
        wanted = list(dict.fromkeys(draft.see_also))
        found = await get_visible_entries(self._store, wanted, writer)
        missing = [t for t in wanted if t not in found]
        if missing:
            raise InvalidInput(f"unknown see_also entries: {', '.join(missing)}")
        resolved = replace(draft, see_also=tuple(dict.fromkeys(found[t].id for t in wanted)))
        return resolved, list({found[t].id: found[t] for t in wanted}.values())

    async def _check_supersedes(self, draft: EntryDraft, writer: Visibility) -> list[Entry]:
        """Reject the write if any supersession target is out of reach;
        otherwise return the targets, for the warnings."""
        denied: list[str] = []
        loaded: list[Entry] = []
        target_ids = list(dict.fromkeys(draft.supersedes))
        # Batched lookups (SP-13), chunked to bound memory.
        for i in range(0, len(target_ids), _SUPERSEDES_LOOKUP_CHUNK):
            chunk = target_ids[i : i + _SUPERSEDES_LOOKUP_CHUNK]
            # Postgres answers with canonical ids (lower case, hyphens):
            # match a target however the caller spelled it.
            targets = {_id_key(e.id): e for e in (await self._store.get_entries(chunk)).values()}
            for target_id in chunk:
                target = targets.get(_id_key(target_id))
                if target is None or not may_supersede(
                    target, new_scope=draft.scope, new_fleet_id=draft.fleet_id, writer=writer
                ):
                    denied.append(target_id)
                else:
                    loaded.append(target)
        if denied:
            raise SupersedeDenied(denied)
        return loaded
