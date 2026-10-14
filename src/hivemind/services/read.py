"""The entry-read path, composed in one place (ADR 0083).

``ReadService`` is the service the surfaces' read paths delegate to: the
visibility-checked read by id (ADR 0033), the batch read (ADRs 0055, 0059)
and the filtered list (SPEC §5.3), each with its feedback summary and
see-also links. It composes ``chain.py``'s building blocks (the
visibility-checked reads, the links, the supersession chain) with the
``GovernanceService``'s feedback summaries; the surfaces serialize only.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from hivemind.config import SearchConfig
from hivemind.domain.access import Visibility
from hivemind.domain.entry import Entry, EntryFilters
from hivemind.domain.feedback import FeedbackSummary
from hivemind.domain.validation import MAX_GET_IDS, InvalidInput
from hivemind.ports import Store
from hivemind.services.chain import (
    EntryLinks,
    entries_links,
    entry_links,
    get_visible_entries,
    get_visible_entry,
    supersession_chain,
)
from hivemind.services.governance import GovernanceService


@dataclass(frozen=True, slots=True)
class EntryDetail:
    """One entry as a read answers it: the entry, its feedback summary,
    its see-also links, and (with history) the supersession chain."""

    entry: Entry
    feedback: FeedbackSummary
    links: EntryLinks
    successors: tuple[Entry, ...] = ()
    superseded: tuple[Entry, ...] = ()


@dataclass(frozen=True, slots=True)
class EntryBatch:
    """A batch read's answer (ADR 0055): the readable entries in the order
    asked, and the ids that name nothing readable (invisible ≡ unknown,
    ADR 0033)."""

    entries: tuple[EntryDetail, ...]
    not_found: tuple[str, ...]


class ReadService:
    """The entry-read path (ADR 0083). Identity is per call, never held."""

    def __init__(
        self,
        store: Store,
        governance: GovernanceService | None = None,
        search_config: SearchConfig | None = None,
    ) -> None:
        self._store = store
        self._governance = governance
        self._search_config = search_config or SearchConfig()

    async def entry(
        self,
        entry_id: str,
        *,
        visibility: Visibility,
        include_history: bool = False,
    ) -> EntryDetail | None:
        """The entry if ``visibility`` may read it, else ``None`` (ADR
        0033: the same answer for "invisible" and "does not exist"), with
        its feedback summary and see-also links, and — with
        ``include_history`` — the supersession chain, which leaves out
        versions the caller may not see."""
        entry = await get_visible_entry(self._store, entry_id, visibility)
        if entry is None:
            return None
        summary, links = await asyncio.gather(
            self._feedback_summary(entry.id),
            entry_links(self._store, entry.id, visibility),
        )
        detail = EntryDetail(entry=entry, feedback=summary, links=links)
        if include_history:
            successors, superseded = await supersession_chain(
                self._store, entry, visibility=visibility
            )
            detail = EntryDetail(
                entry=entry,
                feedback=summary,
                links=links,
                successors=tuple(successors),
                superseded=tuple(superseded),
            )
        return detail

    async def entries(self, entry_ids: Sequence[str], *, visibility: Visibility) -> EntryBatch:
        """Up to ``MAX_GET_IDS`` entries by id in one call (ADR 0055), each
        with its feedback and links; a fixed number of store reads for the
        whole batch (ADR 0059). Ids that name nothing readable are listed
        in ``not_found``. Empty or over-cap is ``InvalidInput``."""
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            raise InvalidInput("entry_ids must name at least one entry")
        if len(ids) > MAX_GET_IDS:
            raise InvalidInput(f"entry_ids may name at most {MAX_GET_IDS} entries")
        found = await get_visible_entries(self._store, ids, visibility)
        entries = [found[eid] for eid in ids if eid in found]
        entry_ids = [e.id for e in entries]
        summaries, links = await asyncio.gather(
            self._feedback_summaries(entry_ids),
            entries_links(self._store, entry_ids, visibility),
        )
        return EntryBatch(
            entries=tuple(
                EntryDetail(entry=e, feedback=summaries[e.id], links=links[e.id]) for e in entries
            ),
            not_found=tuple(eid for eid in ids if eid not in found),
        )

    async def list(
        self,
        filters: EntryFilters,
        *,
        limit: int | None = None,
        offset: int = 0,
        visibility: Visibility,
    ) -> Sequence[Entry]:
        """The filtered list (SPEC §5.3): validates the filters (ADR 0040)
        and applies ``search_config.default_limit`` when ``limit`` is
        omitted; a surface with its own default passes it explicitly."""
        filters.validate()
        effective_limit = limit if limit is not None else self._search_config.default_limit
        return await self._store.list_entries(
            filters, limit=effective_limit, offset=offset, visibility=visibility
        )

    async def _feedback_summary(self, entry_id: str) -> FeedbackSummary:
        if self._governance is None:
            raise InvalidInput("the read service is wired without governance")
        return await self._governance.feedback_summary(entry_id)

    async def _feedback_summaries(self, entry_ids: Sequence[str]) -> dict[str, FeedbackSummary]:
        if self._governance is None:
            raise InvalidInput("the read service is wired without governance")
        return await self._governance.feedback_summaries(list(entry_ids))
