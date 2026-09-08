"""The supersession-chain walk (SPEC.md §5.1 ``?history``, §5.2 ``hive_get``).

A deep module over the ``Store`` port: one small function
(``supersession_chain``) that returns an entry's ``(successors,
superseded)`` — the newer versions it was replaced by, and the older
versions it replaced. Both walks are bounded so a corrupt chain cannot
loop, and the reverse walk uses a single bounded paginated scan because
the v1 stores carry no reverse index (SPEC.md §4.1: ``superseded_by``
is a forward link only).
"""

from __future__ import annotations

from hivemind.domain.entry import Entry, EntryFilters
from hivemind.ports import Store

# Bound on a single forward/reverse chain walk (SPEC.md §6.3: chains are
# short in practice; this is a defensive cap against corrupt data).
_MAX_SUPERSEDE_HOPS = 256
# Page size for the bounded reverse scan.
_REVERSE_SCAN_PAGE = 200


async def supersession_chain(
    store: Store,
    entry: Entry,
    *,
    max_hops: int = _MAX_SUPERSEDE_HOPS,
    reverse_page: int = _REVERSE_SCAN_PAGE,
) -> tuple[list[Entry], list[Entry]]:
    """Return ``(successors, superseded)`` for an entry's chain.

    ``successors`` are the newer versions reachable by following
    ``superseded_by`` forward. ``superseded`` are the older versions this
    entry replaced. Both walks are bounded (``max_hops``) so a corrupt
    chain cannot loop; the reverse walk uses a single bounded paginated
    scan (``reverse_page``) because the v1 stores have no reverse index.
    """
    successors: list[Entry] = []
    cursor = entry
    for _ in range(max_hops):
        next_id = cursor.superseded_by
        if next_id is None:
            break
        nxt = await store.get_entry(next_id)
        if nxt is None:
            break
        successors.append(nxt)
        cursor = nxt

    # No reverse index in v1: build a ``superseded_by -> [entries]`` map
    # with one bounded paginated scan over the inactive entries.
    reverse: dict[str, list[Entry]] = {}
    offset = 0
    while True:
        batch = await store.list_entries(
            EntryFilters(include_inactive=True),
            limit=reverse_page,
            offset=offset,
        )
        if not batch:
            break
        for e in batch:
            if e.superseded_by is not None:
                reverse.setdefault(e.superseded_by, []).append(e)
        if len(batch) < reverse_page:
            break
        offset += reverse_page

    superseded: list[Entry] = []
    frontier = [entry]
    seen = {entry.id}
    for _ in range(max_hops):
        if not frontier:
            break
        next_frontier: list[Entry] = []
        for node in frontier:
            for pred in reverse.get(node.id, ()):
                if pred.id in seen:
                    continue
                superseded.append(pred)
                seen.add(pred.id)
                next_frontier.append(pred)
        frontier = next_frontier

    return successors, superseded
