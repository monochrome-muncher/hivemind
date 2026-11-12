"""Moving a fleet memory to `self` (ADR 0145), in one transaction.

Lock order: the author's cleanup lock, the fleet row (shared, as a fleet write takes it), then the
entry rows by id, so a cleanup of the author, a merge or move of the fleet, and a write superseding
the memory each run wholly before or after the move."""

from __future__ import annotations

from hivemind.domain.access import FleetBusy
from hivemind.domain.self_move import (
    RemovedPin,
    SelfMove,
    replaced_others_refusal,
    self_move_refusal,
)
from hivemind.domain.validation import is_valid_uuid
from hivemind.store.pgstore.agent_cleanup import CLEANUP_AGENT_LOCK
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.fleet_guards import refuse_merged
from hivemind.store.pgstore.rows import row_to_entry
from hivemind.store.pgstore.sql_common import _ENTRY_COLUMNS

LOOKUP = "SELECT author, fleet_id FROM entries WHERE id = $1"

LOCK_HEAD = "SELECT state, scope, author, fleet_id FROM entries WHERE id = $1 FOR UPDATE"

# The memory and every fleet version by its author it replaced, through that author's versions only.
OWN_VERSIONS = """
WITH RECURSIVE own(id) AS (
    SELECT $1::uuid
    UNION
    SELECT e.id FROM entries e JOIN own o ON e.superseded_by = o.id
     WHERE e.author = $2 AND e.scope = 'fleet'
)
SELECT id FROM own
"""

# Other agents' fleet versions one of those replaced: any refuses the move.
OTHERS_REPLACED = """
SELECT id FROM entries
 WHERE superseded_by = ANY($1::uuid[]) AND author <> $2 AND scope = 'fleet'
"""

LOCK_ROWS = "SELECT id FROM entries WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE"

MOVE = (
    "UPDATE entries SET scope = 'self' WHERE id = ANY($1::uuid[]) AND scope = 'fleet' "
    "AND author = $2 RETURNING id"
)

UNPIN = "DELETE FROM pins WHERE entry_id = ANY($1::uuid[]) RETURNING fleet_id, entry_id"

SELECT_MOVED = "SELECT " + _ENTRY_COLUMNS + " FROM entries WHERE id = $1"


class SelfMoveStore(PgConnections):
    async def move_entry_to_self(self, entry_id: str) -> SelfMove:
        if not is_valid_uuid(entry_id):
            raise KeyError(f"unknown entry: {entry_id}")
        async with self._connection() as conn:
            seen = await conn.fetchrow(LOOKUP, entry_id)
        if seen is None:
            raise KeyError(f"unknown entry: {entry_id}")
        fleet_id = str(seen["fleet_id"]) if seen["fleet_id"] is not None else None
        author = seen["author"]
        async with self._write_transaction(fleet_id) as conn:
            await conn.execute(CLEANUP_AGENT_LOCK, author)
            await refuse_merged(conn, fleet_id)
            head = await conn.fetchrow(LOCK_HEAD, entry_id)
            if head is None:
                raise KeyError(f"unknown entry: {entry_id}")
            refusal = self_move_refusal(entry_id, head["state"], head["scope"])
            if refusal is not None:
                raise refusal
            current_fleet = str(head["fleet_id"]) if head["fleet_id"] is not None else None
            if current_fleet != fleet_id:
                raise FleetBusy(fleet_id or "", "the memory changed fleet during the move, retry")
            own = [r["id"] for r in await conn.fetch(OWN_VERSIONS, entry_id, author)]
            await conn.execute(LOCK_ROWS, own)
            others = [str(r["id"]) for r in await conn.fetch(OTHERS_REPLACED, own, author)]
            refusal = replaced_others_refusal(entry_id, others)
            if refusal is not None:
                raise refusal
            moved = {str(r["id"]) for r in await conn.fetch(MOVE, own, author)}
            unpinned = sorted(
                RemovedPin(str(r["fleet_id"]), str(r["entry_id"]))
                for r in await conn.fetch(UNPIN, own)
            )
            row = await conn.fetchrow(SELECT_MOVED, entry_id)
        if row is None:  # unreachable under the row lock
            raise RuntimeError(f"move of entry {entry_id} lost its row")
        entry = row_to_entry(row)
        return SelfMove(
            entry,
            tuple(sorted(moved - {entry.id})),
            tuple(unpinned),
        )
