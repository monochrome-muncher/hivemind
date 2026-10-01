"""The supersession-chain walk (SPEC.md §5.1 ``?history``, §5.2 ``hive_get``).

Also the visibility-checked reads by id (ADR 0033) and "see also" links.
The walk has a version budget (``_MAX_HISTORY_VERSIONS``): since a write
may supersede up to ``MAX_SUPERSEDES`` entries, an agent could otherwise
build a legal tree of thousands of large predecessors for every
``?history`` call to load.
"""

from __future__ import annotations

from dataclasses import dataclass

from hivemind.domain.access import Visibility, entry_is_visible
from hivemind.domain.entry import Entry, EntryState
from hivemind.domain.validation import MAX_ID_CHARS, MAX_LIMIT, MAX_SUPERSEDES
from hivemind.ports import Store

# Bound on a single forward/reverse chain walk (SPEC.md §6.3: chains are
# short in practice; this is a defensive cap against corrupt data).
_MAX_SUPERSEDE_HOPS = 256
# Bound on the versions one walk loads, both directions together, visible
# or not: never more than one list page (``limit`` <= MAX_LIMIT).
_MAX_HISTORY_VERSIONS = MAX_LIMIT
# Incoming "see also" links shown on one read (ADR 0057): a popular entry
# can be linked from many, and the newest links matter most.
MAX_LINKED_FROM = 20


async def get_visible_entry(store: Store, entry_id: str, visibility: Visibility) -> Entry | None:
    """The entry with ``entry_id`` if ``visibility`` may read it, else
    ``None`` (ADR 0033). "Not visible" and "does not exist" are the same
    answer, so an id reveals nothing, not even existence."""
    if "\x00" in entry_id or len(entry_id) > MAX_ID_CHARS:
        return None  # cannot name an entry (and Postgres cannot hold a NUL)
    entry = await store.get_entry(entry_id)
    if entry is None or not entry_is_visible(entry, visibility):
        return None
    return entry


async def get_visible_entries(
    store: Store, entry_ids: list[str], visibility: Visibility
) -> dict[str, Entry]:
    """``get_visible_entry`` for several ids in one store read (ADR 0055):
    the readable entries by id. An id that names nothing readable is simply
    absent, the same answer for "invisible" and "does not exist"."""
    ids = [eid for eid in entry_ids if "\x00" not in eid and len(eid) <= MAX_ID_CHARS]
    if not ids:
        return {}
    found = await store.get_entries(ids)
    result: dict[str, Entry] = {}
    for eid in ids:
        # Postgres answers with canonical (lower-case) uuids; key the result
        # by the id as asked, so a caller's spelling still finds its entry.
        entry = found.get(eid) or found.get(eid.lower())
        if entry is not None and entry_is_visible(entry, visibility):
            result[eid] = entry
    return result


@dataclass(frozen=True, slots=True)
class EntryLinks:
    """An entry's "see also" links as one reader sees them (ADR 0057)."""

    see_also: tuple[Entry, ...] = ()
    linked_from: tuple[Entry, ...] = ()


async def entry_links(store: Store, entry_id: str, visibility: Visibility) -> EntryLinks:
    """The entries ``entry_id`` links to and the active entries that link
    to it, limited to what ``visibility`` may read (ADR 0057). A linked
    entry the reader may not see is left out, as if the link did not
    exist; ``linked_from`` holds at most ``MAX_LINKED_FROM``, newest link
    first."""
    outgoing, incoming = await store.entry_links(entry_id, MAX_LINKED_FROM)
    if not outgoing and not incoming:
        return EntryLinks()
    found = await get_visible_entries(store, list(dict.fromkeys(outgoing + incoming)), visibility)
    return EntryLinks(
        see_also=tuple(found[i] for i in outgoing if i in found),
        linked_from=tuple(
            found[i] for i in incoming if i in found and found[i].state is EntryState.ACTIVE
        ),
    )


async def supersession_chain(
    store: Store,
    entry: Entry,
    *,
    visibility: Visibility | None = None,
    max_hops: int = _MAX_SUPERSEDE_HOPS,
    max_versions: int = _MAX_HISTORY_VERSIONS,
) -> tuple[list[Entry], list[Entry]]:
    """Return ``(successors, superseded)`` for an entry's chain.

    ``successors`` are the newer versions, ``superseded`` the older ones.
    Both walks are bounded by ``max_hops``. At most ``max_versions`` are
    loaded in all, successors first, then predecessors breadth-first; a
    longer chain comes back truncated.

    With ``visibility``, unreadable versions are walked through but not
    returned (ADR 0033); ``None`` returns everything (internal callers only).
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
            # A corrupt cycle: stop (the reverse walk dedups the same way).
            break
        successors.append(nxt)
        seen.add(nxt.id)
        cursor = nxt

    # Reverse walk, frontier by frontier: one indexed ``list_predecessors``
    # call per hop (migration 0008), so cost follows the chain, not the
    # pool. A version has at most MAX_SUPERSEDES predecessors, so batches
    # of ``budget // MAX_SUPERSEDES`` never load much more than the budget.
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
        # Stable order regardless of the store's row order.
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
