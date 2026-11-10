"""The supersession-chain walk (SPEC.md §5.1 ``?history``, §5.2 ``hive_get``), the
visibility-checked reads by id (ADR 0033) and "see also" links.

The walk has a version budget (``_MAX_HISTORY_VERSIONS``): a write may supersede up to
``MAX_SUPERSEDES`` entries, so an agent could otherwise build a legal tree of thousands of large
predecessors for every ``?history`` call to load.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from hivemind.domain.access import Visibility, entry_is_visible
from hivemind.domain.entry import Entry, EntryState
from hivemind.domain.validation import MAX_ID_CHARS, MAX_LIMIT, MAX_SUPERSEDES, canonical_uuid
from hivemind.ports import Store

# Defensive cap on one chain walk against corrupt data (chains are short, SPEC.md §6.3).
_MAX_SUPERSEDE_HOPS = 256
# Versions one walk loads, both directions, visible or not: one list page.
_MAX_HISTORY_VERSIONS = MAX_LIMIT
# Incoming "see also" links shown on one read, newest first (ADR 0057).
MAX_LINKED_FROM = 20


def _can_name_entry(entry_id: str) -> bool:
    """Whether ``entry_id`` could name an entry (Postgres cannot hold a NUL)."""
    return "\x00" not in entry_id and len(entry_id) <= MAX_ID_CHARS


async def get_visible_entry(store: Store, entry_id: str, visibility: Visibility) -> Entry | None:
    """The entry if ``visibility`` may read it, else ``None`` (ADR 0033): "not visible" and "does
    not exist" are the same answer, so an id reveals nothing, not even existence."""
    if not _can_name_entry(entry_id):
        return None
    entry = await store.get_entry(entry_id)
    if entry is None or not entry_is_visible(entry, visibility):
        return None
    return entry


async def get_visible_entries(
    store: Store, entry_ids: list[str], visibility: Visibility
) -> dict[str, Entry]:
    """``get_visible_entry`` for several ids in one store read (ADR 0055); unreadable ids are
    absent."""
    ids = [eid for eid in entry_ids if _can_name_entry(eid)]
    if not ids:
        return {}
    found = await store.get_entries(ids)
    result: dict[str, Entry] = {}
    for eid in ids:
        # The store answers with canonical ids; key by the id as asked so any spelling finds it.
        entry = found.get(canonical_uuid(eid) or eid)
        if entry is not None and entry_is_visible(entry, visibility):
            result[eid] = entry
    return result


async def hide_unseen_successors(
    store: Store, entries: Sequence[Entry], visibility: Visibility
) -> list[Entry]:
    """``entries`` with ``superseded_by`` cleared where the successor is unreadable (ADR 0033), so
    a reader never gets an id it cannot open. One batched store read."""
    wanted = list(
        dict.fromkeys(
            e.superseded_by
            for e in entries
            if e.state is EntryState.SUPERSEDED and e.superseded_by is not None
        )
    )
    if not wanted:
        return list(entries)
    seen = await get_visible_entries(store, wanted, visibility)
    return [
        replace(e, superseded_by=None)
        if e.state is EntryState.SUPERSEDED
        and e.superseded_by is not None
        and e.superseded_by not in seen
        else e
        for e in entries
    ]


@dataclass(frozen=True, slots=True)
class EntryLinks:
    """An entry's "see also" links as one reader sees them (ADR 0057)."""

    see_also: tuple[Entry, ...] = ()
    linked_from: tuple[Entry, ...] = ()


def _links_of(outgoing: list[str], incoming: list[str], found: dict[str, Entry]) -> EntryLinks:
    """The links among ``found`` (the readable ones); incoming only while active."""
    return EntryLinks(
        see_also=tuple(found[i] for i in outgoing if i in found),
        linked_from=tuple(
            found[i] for i in incoming if i in found and found[i].state is EntryState.ACTIVE
        ),
    )


async def entry_links(store: Store, entry_id: str, visibility: Visibility) -> EntryLinks:
    """The entries ``entry_id`` links to and the active ones linking to it, limited to what
    ``visibility`` may read (ADR 0057); an unreadable one is left out as if the link did not
    exist."""
    outgoing, incoming = await store.entry_links(entry_id, MAX_LINKED_FROM, visibility=visibility)
    if not outgoing and not incoming:
        return EntryLinks()
    found = await get_visible_entries(store, list(dict.fromkeys(outgoing + incoming)), visibility)
    return _links_of(outgoing, incoming, found)


async def entries_links(
    store: Store, entry_ids: list[str], visibility: Visibility
) -> dict[str, EntryLinks]:
    """``entry_links`` for several entries, two store reads however many (ADR 0059)."""
    if not entry_ids:
        return {}
    raw = await store.entry_links_many(entry_ids, MAX_LINKED_FROM, visibility=visibility)
    linked = list(dict.fromkeys(i for out, inc in raw.values() for i in out + inc))
    found = await get_visible_entries(store, linked, visibility) if linked else {}
    return {eid: _links_of(outgoing, incoming, found) for eid, (outgoing, incoming) in raw.items()}


async def supersession_chain(
    store: Store,
    entry: Entry,
    *,
    visibility: Visibility | None = None,
    max_hops: int = _MAX_SUPERSEDE_HOPS,
    max_versions: int = _MAX_HISTORY_VERSIONS,
) -> tuple[list[Entry], list[Entry]]:
    """``(successors, superseded)`` for an entry's chain: newer and older versions.

    Walks are bounded by ``max_hops``; at most ``max_versions`` load in all, successors first, then
    predecessors breadth-first, so a longer chain comes back truncated. With ``visibility``,
    unreadable versions are walked through but not returned (ADR 0033); ``None`` returns everything
    (internal callers only).
    """
    successors: list[Entry] = []
    cursor = entry
    seen = {entry.id}
    for _ in range(min(max_hops, max_versions)):
        next_id = cursor.superseded_by
        if next_id is None:
            break
        nxt = await store.get_entry(next_id)
        if nxt is None:
            break
        if nxt.id in seen:
            # A corrupt cycle.
            break
        successors.append(nxt)
        seen.add(nxt.id)
        cursor = nxt

    # Reverse walk, one indexed ``list_predecessors`` call per hop. A version has at most
    # MAX_SUPERSEDES predecessors, so batches of ``budget // MAX_SUPERSEDES`` stay near the budget.
    superseded: list[Entry] = []
    frontier = [entry]
    seen = {entry.id}
    budget = max_versions - len(successors)
    for _ in range(max_hops):
        if not frontier or budget <= 0:
            break
        preds: list[Entry] = []
        while frontier and len(preds) < budget:
            batch_size = max(1, (budget - len(preds)) // MAX_SUPERSEDES)
            batch, frontier = frontier[:batch_size], frontier[batch_size:]
            preds.extend(await store.list_predecessors([node.id for node in batch]))
        # Stable regardless of the store's row order.
        preds.sort(key=lambda e: (e.created_at, e.id), reverse=True)
        frontier = []
        for pred in preds:
            if pred.id in seen:
                continue
            if budget <= 0:
                break
            superseded.append(pred)
            seen.add(pred.id)
            frontier.append(pred)
            budget -= 1

    if visibility is not None:
        successors = [e for e in successors if entry_is_visible(e, visibility)]
        superseded = [e for e in superseded if entry_is_visible(e, visibility)]
    return successors, superseded
