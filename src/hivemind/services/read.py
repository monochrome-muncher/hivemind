"""The entry-read path (ADR 0083): read by id, batch read (ADRs 0055, 0059) and filtered list
(SPEC §5.3), each with feedback summary and see-also links; the surfaces only serialize.

An entry names a successor only if the reader can read it (ADR 0033).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, replace

from hivemind.config import SearchConfig
from hivemind.domain.access import Visibility
from hivemind.domain.entry import Entry, EntryFilters
from hivemind.domain.feedback import FeedbackCounts, FeedbackSummary
from hivemind.domain.validation import MAX_GET_IDS, InvalidInput
from hivemind.ports import Credential, Store
from hivemind.services.chain import (
    EntryLinks,
    entries_links,
    entry_links,
    get_visible_entries,
    get_visible_entry,
    hide_unseen_successors,
    supersession_chain,
)
from hivemind.services.errors import AgentIdentityRequired
from hivemind.services.feedback import reporter_agent
from hivemind.services.governance import GovernanceService
from hivemind.services.ownership import own_author_filter


@dataclass(frozen=True, slots=True)
class EntryDetail:
    """One entry as a read answers it, with the supersession chain when history is asked for."""

    entry: Entry
    feedback: FeedbackSummary
    links: EntryLinks
    successors: tuple[Entry, ...] = ()
    superseded: tuple[Entry, ...] = ()


@dataclass(frozen=True, slots=True)
class EntryBatch:
    """A batch read's answer (ADR 0055): readable entries in the order asked, and the ids that name
    nothing readable."""

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
        """The entry if ``visibility`` may read it, else ``None`` (ADR 0033), with feedback and
        links; ``include_history`` adds the chain, without versions the caller may not see."""
        entry = await get_visible_entry(self._store, entry_id, visibility)
        if entry is None:
            return None
        summary, links = await asyncio.gather(
            self._feedback_summary(entry.id),
            entry_links(self._store, entry.id, visibility),
        )
        successors: list[Entry] = []
        superseded: list[Entry] = []
        if include_history:
            successors, superseded = await supersession_chain(
                self._store, entry, visibility=visibility
            )
        # One lookup for every version shown.
        shown = await hide_unseen_successors(
            self._store, [entry, *successors, *superseded], visibility
        )
        return EntryDetail(
            entry=shown[0],
            feedback=summary,
            links=links,
            successors=tuple(shown[1 : 1 + len(successors)]),
            superseded=tuple(shown[1 + len(successors) :]),
        )

    async def entries(self, entry_ids: Sequence[str], *, visibility: Visibility) -> EntryBatch:
        """Up to ``MAX_GET_IDS`` entries by id (ADR 0055) in a fixed number of store reads
        (ADR 0059); unreadable ids go to ``not_found``. Empty or over-cap is ``InvalidInput``."""
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            raise InvalidInput("entry_ids must name at least one entry")
        if len(ids) > MAX_GET_IDS:
            raise InvalidInput(f"entry_ids may name at most {MAX_GET_IDS} entries")
        found = await get_visible_entries(self._store, ids, visibility)
        # Keyed by entry id: two spellings of one uuid name the same entry, returned once.
        entries = await hide_unseen_successors(
            self._store,
            list({found[eid].id: found[eid] for eid in ids if eid in found}.values()),
            visibility,
        )
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
        credential: Credential | None = None,
    ) -> Sequence[Entry]:
        """The filtered list (SPEC §5.3); ``search_config.default_limit`` applies when ``limit`` is
        omitted. ``relied_on_changed`` (ADR 0098) needs the caller's feedback identity, else
        ``AgentIdentityRequired``."""
        filters.validate()
        if filters.relied_on_changed:
            agent = reporter_agent(credential, None) if credential is not None else None
            if credential is None or agent is None:
                raise AgentIdentityRequired(
                    "relied_on_changed needs the caller's agent identity (an agent or admin key)"
                )
            filters = replace(filters, reporter=(credential.user_id, agent))
        filters = own_author_filter(filters, visibility)
        effective_limit = limit if limit is not None else self._search_config.default_limit
        listed = await self._store.list_entries(
            filters, limit=effective_limit, offset=offset, visibility=visibility
        )
        return await hide_unseen_successors(self._store, listed, visibility)

    async def feedback_counts(self, entries: Sequence[Entry]) -> dict[str, FeedbackCounts]:
        """Live feedback counts per entry in one store read (for ``relied_on_changed`` list
        items)."""
        return await self._store.quality_counts([e.id for e in entries])

    async def _feedback_summary(self, entry_id: str) -> FeedbackSummary:
        if self._governance is None:
            raise InvalidInput("the read service is wired without governance")
        return await self._governance.feedback_summary(entry_id)

    async def _feedback_summaries(self, entry_ids: Sequence[str]) -> dict[str, FeedbackSummary]:
        if self._governance is None:
            raise InvalidInput("the read service is wired without governance")
        return await self._governance.feedback_summaries(list(entry_ids))
