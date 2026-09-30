"""The supersession-chain walk (SPEC.md §5.1 ``?history``, §5.2 ``hive_get``).

A deep module over the ``Store`` port: one small function
(``supersession_chain``) that returns an entry's ``(successors,
superseded)`` — the newer versions it was replaced by, and the older
versions it replaced. Both walks are bounded so a corrupt chain cannot
loop, and the reverse walk asks the store for each frontier's
predecessors (``Store.list_predecessors``, served by the partial
``superseded_by`` index of migration 0008) — one query per hop, so the
cost follows the chain, not the pool.
"""

from __future__ import annotations

from hivemind.domain.access import Visibility, entry_is_visible
from hivemind.domain.entry import Entry
from hivemind.domain.validation import MAX_ID_CHARS
from hivemind.ports import Store

# Bound on a single forward/reverse chain walk (SPEC.md §6.3: chains are
# short in practice; this is a defensive cap against corrupt data).
_MAX_SUPERSEDE_HOPS = 256


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


async def supersession_chain(
    store: Store,
    entry: Entry,
    *,
    visibility: Visibility | None = None,
    max_hops: int = _MAX_SUPERSEDE_HOPS,
) -> tuple[list[Entry], list[Entry]]:
    """Return ``(successors, superseded)`` for an entry's chain.

    ``successors`` are the newer versions reachable by following
    ``superseded_by`` forward. ``superseded`` are the older versions this
    entry replaced. Both walks are bounded (``max_hops``) so a corrupt
    chain cannot loop; the reverse walk is one indexed
    ``Store.list_predecessors`` call per hop.

    With ``visibility``, versions that reader may not see are left out
    (ADR 0033) — the walk still passes through them, it just never
    returns them. ``None`` returns everything (internal callers only).
    """
    successors: list[Entry] = []
    cursor = entry
    seen = {entry.id}
    for _ in range(max_hops):
        next_id = cursor.superseded_by
        if next_id is None:
            break
        nxt = await store.get_entry(next_id)
        if nxt is None:
            break
        if nxt.id in seen:
            # A corrupt ``superseded_by`` cycle: stop rather than loop
            # (the walk is bounded anyway, but dedup keeps the result
            # sane — the reverse walk dedups the same way).
            break
        successors.append(nxt)
        seen.add(nxt.id)
        cursor = nxt

    # Reverse walk, frontier by frontier: one indexed ``list_predecessors``
    # call per hop (migration 0008's partial ``superseded_by`` index), so the
    # cost follows the chain's depth, never the pool's size. Unfiltered on
    # purpose: the walk passes through versions the reader cannot see.
    superseded: list[Entry] = []
    frontier = [entry]
    seen = {entry.id}
    for _ in range(max_hops):
        if not frontier:
            break
        preds = await store.list_predecessors([node.id for node in frontier])
        # Stable order regardless of the store's row order.
        preds.sort(key=lambda e: (e.created_at, e.id), reverse=True)
        frontier = []
        for pred in preds:
            if pred.id in seen:
                continue
            superseded.append(pred)
            seen.add(pred.id)
            frontier.append(pred)

    if visibility is not None:
        successors = [e for e in successors if entry_is_visible(e, visibility)]
        superseded = [e for e in superseded if entry_is_visible(e, visibility)]
    return successors, superseded
