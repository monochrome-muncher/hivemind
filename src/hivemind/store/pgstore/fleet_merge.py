"""Fleet merges and moves, their undo and the duplicate scan."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime
from typing import NamedTuple, cast

import asyncpg

from hivemind.domain.access import FleetExists, FleetMerged, UnknownFleet
from hivemind.domain.fleet_merge import (
    EXACT_DUPLICATE_LOOKUP_MAX,
    KIND_MERGE,
    KIND_MOVE,
    DroppedPin,
    DuplicatePair,
    DuplicateScan,
    FleetMergeRecord,
    FleetMergeRefused,
    FleetMoveRefused,
    MergeOutcome,
    MoveOutcome,
    MovePlan,
    UnknownFleetMerge,
    UnmergeOutcome,
    check_pins_unchanged,
    choose_pins,
    plan_pin_restore,
    undo_refusal,
)
from hivemind.domain.pin import Pin
from hivemind.domain.validation import InvalidInput, is_valid_uuid
from hivemind.store.pgstore.connection import (
    LONG_OP_TIMEOUT,
    MERGE_SETTINGS,
    PgConnections,
    refuse_on_contention,
)
from hivemind.store.pgstore.fleets import GET_FLEET, INSERT_FLEET, SELECT_FLEET_BY_NAME
from hivemind.store.pgstore.pin_common import (
    _PIN_COLUMNS,
    _PIN_ORDER,
    LOCK_FLEET_PINS,
    PINNED_ENTRY_IDS,
    RESTORE_PIN,
    restore_fleet_pins,
)
from hivemind.store.pgstore.rows import dropped_json, row_to_fleet, row_to_merge, row_to_pin
from hivemind.store.pgstore.sql_common import VECTOR_SEARCH_SETTINGS

LOCK_FLEETS = """
SELECT f.id, f.name, f.created_at, f.merged_into, f.merged_at, f.phase, f.phase_until,
       t.name AS target_name
  FROM fleets f LEFT JOIN fleets t ON t.id = f.merged_into
 WHERE f.id = ANY($1::uuid[]) ORDER BY f.id FOR UPDATE OF f
"""

PINS_OF_FLEETS = f"""
SELECT {_PIN_COLUMNS} FROM pins WHERE fleet_id = ANY($1::text[])
 ORDER BY {_PIN_ORDER}
"""

MOVE_ENTRIES = "UPDATE entries SET fleet_id = $2 WHERE fleet_id = $1 RETURNING id"

RECORD_MERGED_ENTRIES = """
INSERT INTO fleet_merge_entries (merge_id, entry_id) SELECT $1, unnest($2::uuid[])
"""

MOVE_AGENTS = "UPDATE agents SET home_fleet_id = $2 WHERE home_fleet_id = $1 RETURNING name"

DELETE_PINS_OF = "DELETE FROM pins WHERE fleet_id = $1 AND entry_id = ANY($2::uuid[])"

REKEY_PINS = "UPDATE pins SET fleet_id = $2 WHERE fleet_id = $1"

FOLD_SEARCH_COUNTS = """
INSERT INTO search_counts (fleet_id, searches, empty, explore)
SELECT $2, searches, empty, explore FROM search_counts WHERE fleet_id = $1
ON CONFLICT (fleet_id) DO UPDATE
   SET searches = search_counts.searches + EXCLUDED.searches,
       empty = search_counts.empty + EXCLUDED.empty,
       explore = search_counts.explore + EXCLUDED.explore
"""

DELETE_SEARCH_COUNTS = "DELETE FROM search_counts WHERE fleet_id = $1"

MARK_MERGED = (
    "UPDATE fleets SET merged_into = $2, merged_at = clock_timestamp() "
    "WHERE id = $1 RETURNING merged_at"
)

INSERT_MERGE = """
INSERT INTO fleet_merges (id, source_fleet_id, target_fleet_id, merged_by, merged_at,
                          moved_agents, dropped_pins, kind)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""

_MERGE_COLUMNS = (
    "id, source_fleet_id, target_fleet_id, merged_by, merged_at, moved_agents, "
    "dropped_pins, undone_at, kind"
)

GET_MERGE = f"SELECT {_MERGE_COLUMNS} FROM fleet_merges WHERE id = $1"

LIST_MERGES = f"SELECT {_MERGE_COLUMNS} FROM fleet_merges ORDER BY merged_at DESC, id DESC LIMIT $1"

MERGES_INVOLVING = f"""
SELECT {_MERGE_COLUMNS} FROM fleet_merges
 WHERE source_fleet_id = ANY($1::uuid[]) OR target_fleet_id = ANY($1::uuid[])
 ORDER BY merged_at DESC, id DESC
"""

RESTORE_ENTRIES = """
UPDATE entries SET fleet_id = $2
 WHERE fleet_id = $3 AND id IN (SELECT entry_id FROM fleet_merge_entries WHERE merge_id = $1)
RETURNING id
"""

RESTORE_AGENTS = """
UPDATE agents SET home_fleet_id = $2 WHERE name = ANY($1::text[]) AND home_fleet_id = $3
RETURNING name
"""

RETURN_PINS = """
UPDATE pins SET fleet_id = $2
 WHERE fleet_id = $3 AND entry_id IN (SELECT entry_id FROM fleet_merge_entries WHERE merge_id = $1)
RETURNING entry_id
"""

ENTRIES_IN_FLEET = "SELECT id FROM entries WHERE id = ANY($1::uuid[]) AND fleet_id = $2"

MOVE_RETURNING_PINS = f"""
SELECT {_PIN_COLUMNS} FROM pins
 WHERE fleet_id = $1 AND entry_id = ANY($2::uuid[])
"""

MOVE_PIN_HOME = "UPDATE pins SET fleet_id = $2 WHERE fleet_id = $1 AND entry_id = $3"

CLEAR_MERGED = "UPDATE fleets SET merged_into = NULL, merged_at = NULL WHERE id = $1"

MARK_UNDONE = (
    "UPDATE fleet_merges SET undone_at = clock_timestamp() WHERE id = $1 RETURNING undone_at"
)

# The movers' fleet entries whose supersession chain also ends in one of
# theirs (`plan_move_entries` is the same rule over objects): walk each
# candidate's `superseded_by` links to the head (the entry nothing supersedes).
PLAN_MOVE = """
WITH RECURSIVE cand AS (
    SELECT id FROM entries
     WHERE fleet_id = $1 AND scope = 'fleet' AND author = ANY($2::text[])
), walk(origin, cur) AS (
    SELECT id, id FROM cand
    UNION
    SELECT w.origin, e.superseded_by FROM walk w JOIN entries e ON e.id = w.cur
     WHERE e.superseded_by IS NOT NULL
)
SELECT e.id, e.state FROM entries e
 WHERE e.id IN (
    SELECT w.origin FROM walk w JOIN entries h ON h.id = w.cur AND h.superseded_by IS NULL
     WHERE w.cur IN (SELECT id FROM cand))
"""

# Locks the movers' fleet entries so an admin org-supersede (which takes no
# fleet lock) cannot flip a chain head between the plan and the update.
LOCK_MOVER_ENTRIES = (
    "SELECT id FROM entries "
    "WHERE fleet_id = $1 AND scope = 'fleet' AND author = ANY($2::text[]) ORDER BY id FOR UPDATE"
)

COUNT_MOVER_ENTRIES = (
    "SELECT count(*) FROM entries "
    "WHERE fleet_id = $1 AND scope = 'fleet' AND author = ANY($2::text[])"
)

LOCK_AGENTS = (
    "SELECT name, home_fleet_id FROM agents WHERE name = ANY($1::text[]) ORDER BY name FOR UPDATE"
)

MOVE_THESE_ENTRIES = (
    "UPDATE entries SET fleet_id = $2 WHERE fleet_id = $1 AND id = ANY($3::uuid[]) RETURNING id"
)

MOVE_THESE_AGENTS = (
    "UPDATE agents SET home_fleet_id = $2 WHERE name = ANY($1::text[]) AND home_fleet_id = $3 "
    "RETURNING name"
)

MOVE_PIN_CANDIDATES = f"""
SELECT {_PIN_COLUMNS} FROM pins
 WHERE (fleet_id = $1 AND entry_id = ANY($3::uuid[])) OR fleet_id = $2
 ORDER BY {_PIN_ORDER}
"""

REKEY_THESE_PINS = (
    "UPDATE pins SET fleet_id = $2 WHERE fleet_id = $1 AND entry_id = ANY($3::uuid[])"
)

DUPLICATE_PAIRS = """
SELECT a.id AS entry_id, n.id AS match_id, n.sim
  FROM (SELECT id, embedding FROM entries
         WHERE fleet_id = $1 AND state = 'active' AND embedding IS NOT NULL
         ORDER BY created_at DESC, id LIMIT $3) a
 CROSS JOIN LATERAL (
       SELECT b.id, 1 - (b.embedding <=> a.embedding) AS sim FROM entries b
        WHERE b.fleet_id = $2 AND b.state = 'active' AND b.embedding IS NOT NULL
        ORDER BY b.embedding <=> a.embedding LIMIT 1) n
 WHERE n.sim >= $4
 ORDER BY n.sim DESC, a.id
"""

# The same pairs when the fleet looked into ($2) is small: its vectors are
# read once (no fleet_id index, ADR 0126, so the HNSW scan would be filtered)
# and each lookup is an exact distance sort over them.
DUPLICATE_PAIRS_EXACT = """
WITH b AS MATERIALIZED (
       SELECT id, embedding FROM entries
        WHERE fleet_id = $2 AND state = 'active' AND embedding IS NOT NULL)
SELECT a.id AS entry_id, n.id AS match_id, n.sim
  FROM (SELECT id, embedding FROM entries
         WHERE fleet_id = $1 AND state = 'active' AND embedding IS NOT NULL
         ORDER BY created_at DESC, id LIMIT $3) a
 CROSS JOIN LATERAL (
       SELECT b.id, 1 - (b.embedding <=> a.embedding) AS sim FROM b
        ORDER BY b.embedding <=> a.embedding LIMIT 1) n
 WHERE n.sim >= $4
 ORDER BY n.sim DESC, a.id
"""

# A preview's lookups stay under the pool's command timeout (30 s): the
# server cancels the scan first, and each lookup visits a bounded graph part.
DUPLICATE_SCAN_SETTINGS = """
SET LOCAL statement_timeout = '20s';
SET LOCAL hnsw.max_scan_tuples = 20000;
"""

COUNT_DUPLICATE_CANDIDATES = (
    "SELECT count(*) FROM entries "
    "WHERE fleet_id = $1 AND state = 'active' AND embedding IS NOT NULL"
)

# Counts up to $2 + 1 of the same rows, so a big fleet is not counted whole.
COUNT_DUPLICATE_CANDIDATES_UPTO = (
    "SELECT count(*) FROM (SELECT 1 FROM entries "
    "WHERE fleet_id = $1 AND state = 'active' AND embedding IS NOT NULL LIMIT $2 + 1) c"
)


def _fleets_contention(refusal: type[Exception], verb: str) -> AbstractContextManager[None]:
    """``refuse_on_contention`` with the fleet merge, move and undo messages."""
    return refuse_on_contention(
        refusal,
        f"the fleets are busy with writes; try the {verb} again in a moment",
        f"the {verb} deadlocked with a concurrent change; try it again",
    )


def _require_one_target(target_fleet_id: str | None, new_fleet_name: str | None) -> None:
    if (target_fleet_id is None) == (new_fleet_name is None):
        raise InvalidInput("name exactly one of target_fleet_id and new_fleet_name")


def _canonical_fleet_ids(
    source_id: str, target_fleet_id: str | None, into_itself: Exception
) -> tuple[str, str | None]:
    """The merge or move's fleet ids as canonical uuid text; ``into_itself`` is
    raised when the target is the source."""
    for given in (source_id, target_fleet_id):
        if given is not None and not is_valid_uuid(given):
            raise UnknownFleet(f"unknown fleet: {given}")
    source_id = str(uuid.UUID(source_id))
    if target_fleet_id is not None:
        target_fleet_id = str(uuid.UUID(target_fleet_id))
        if target_fleet_id == source_id:
            raise into_itself
    return source_id, target_fleet_id


async def _create_target_fleet(
    conn: asyncpg.Connection, source_id: str, new_fleet_name: str | None
) -> str:
    """Create the new fleet a merge or move targets; an unknown source wins over a
    taken name (as in MemoryStore)."""
    if await conn.fetchrow(GET_FLEET, source_id) is None:
        raise UnknownFleet(f"unknown fleet: {source_id}")
    if await conn.fetchrow(SELECT_FLEET_BY_NAME, new_fleet_name) is not None:
        raise FleetExists(f"fleet already exists: {new_fleet_name!r}")
    made = await conn.fetchrow(INSERT_FLEET, new_fleet_name)
    if made is None:
        raise FleetExists(f"fleet already exists: {new_fleet_name!r}")
    return str(made["id"])


async def _lock_unmerged_fleets(
    conn: asyncpg.Connection, source_id: str, target_fleet_id: str
) -> dict[str, asyncpg.Record]:
    """Both fleet rows FOR UPDATE, in id order; ``UnknownFleet`` for a missing one,
    ``FleetMerged`` for one already merged. Returns the rows by id."""
    locked = {str(r["id"]): r for r in await conn.fetch(LOCK_FLEETS, [source_id, target_fleet_id])}
    for fid in (source_id, target_fleet_id):
        if fid not in locked:
            raise UnknownFleet(f"unknown fleet: {fid}")
    for fid in (source_id, target_fleet_id):
        row = locked[fid]
        if row["merged_into"] is not None:
            raise FleetMerged(fid, str(row["merged_into"]), row["target_name"])
    return locked


async def _lock_fleet_pins(conn: asyncpg.Connection, source_id: str, target_fleet_id: str) -> None:
    for fid in sorted((source_id, target_fleet_id)):
        await conn.execute(LOCK_FLEET_PINS, fid)


async def _delete_dropped_pins(
    conn: asyncpg.Connection, dropped: Sequence[Pin], source_id: str, target_fleet_id: str
) -> None:
    for fid in (source_id, target_fleet_id):
        gone = [p.entry_id for p in dropped if p.fleet_id == fid]
        if gone:
            await conn.execute(DELETE_PINS_OF, fid, gone)


def _dropped_pins(dropped: Sequence[Pin]) -> tuple[DroppedPin, ...]:
    return tuple(DroppedPin(p.fleet_id, p.entry_id, p.pinned_by, p.pinned_at) for p in dropped)


async def _record_merge(
    conn: asyncpg.Connection,
    *,
    merge_id: str,
    source_id: str,
    target_fleet_id: str,
    done_by: str,
    done_at: datetime,
    agents: Sequence[str],
    drops: Sequence[DroppedPin],
    kind: str,
) -> None:
    await conn.execute(
        INSERT_MERGE,
        merge_id,
        source_id,
        target_fleet_id,
        done_by,
        done_at,
        agents,
        dropped_json(drops),
        kind,
    )


async def _lock_and_load_pins(
    conn: asyncpg.Connection, source_id: str, target_fleet_id: str
) -> list[Pin]:
    """Both fleets' pin locks, then their pins."""
    # Pins last-but-one in the lock order: fleet rows, then pin locks.
    await _lock_fleet_pins(conn, source_id, target_fleet_id)
    return [row_to_pin(r) for r in await conn.fetch(PINS_OF_FLEETS, [source_id, target_fleet_id])]


async def _move_all_entries(
    conn: asyncpg.Connection, merge_id: str, source_id: str, target_fleet_id: str
) -> list[str]:
    """Move every entry of the source to the target and record which; returns their ids."""
    moved = [
        str(r["id"])
        for r in await conn.fetch(MOVE_ENTRIES, source_id, target_fleet_id, timeout=LONG_OP_TIMEOUT)
    ]
    if moved:
        await conn.execute(RECORD_MERGED_ENTRIES, merge_id, moved, timeout=LONG_OP_TIMEOUT)
    return moved


async def _fold_source_into_target(
    conn: asyncpg.Connection, source_id: str, target_fleet_id: str
) -> datetime:
    """Re-key the source's pins and fold its search counts into the target, then mark the
    source merged; returns when."""
    await conn.execute(REKEY_PINS, source_id, target_fleet_id)
    await conn.execute(FOLD_SEARCH_COUNTS, source_id, target_fleet_id)
    await conn.execute(DELETE_SEARCH_COUNTS, source_id)
    return cast(datetime, await conn.fetchval(MARK_MERGED, source_id, target_fleet_id))


async def _lock_source_agents(
    conn: asyncpg.Connection, names: Sequence[str], source_id: str, source_name: str
) -> None:
    """Lock the agents; ``FleetMoveRefused`` unless every one is an agent of the source."""
    homes = {r["name"]: r["home_fleet_id"] for r in await conn.fetch(LOCK_AGENTS, names)}
    away = [n for n in names if str(homes.get(n)) != source_id]
    if away:
        raise FleetMoveRefused(
            f"not agents of fleet {source_name!r} (or unknown): " + ", ".join(away)
        )


async def _lock_and_plan_movers(
    conn: asyncpg.Connection, source_id: str, names: Sequence[str]
) -> tuple[list[str], int]:
    """Lock the agents' entries; returns (ids of those that move, how many are held back)."""
    await conn.fetch(LOCK_MOVER_ENTRIES, source_id, names, timeout=LONG_OP_TIMEOUT)
    moving_rows = await conn.fetch(PLAN_MOVE, source_id, names, timeout=LONG_OP_TIMEOUT)
    moving = [str(r["id"]) for r in moving_rows]
    total = await conn.fetchval(COUNT_MOVER_ENTRIES, source_id, names, timeout=LONG_OP_TIMEOUT)
    return moving, int(total) - len(moving)


async def _move_these_entries(
    conn: asyncpg.Connection,
    move_id: str,
    source_id: str,
    target_fleet_id: str,
    moving: list[str],
) -> None:
    if moving:
        await conn.execute(
            MOVE_THESE_ENTRIES,
            source_id,
            target_fleet_id,
            moving,
            timeout=LONG_OP_TIMEOUT,
        )
        await conn.execute(RECORD_MERGED_ENTRIES, move_id, moving, timeout=LONG_OP_TIMEOUT)


async def _load_undoable_merge(conn: asyncpg.Connection, merge_id: str) -> FleetMergeRecord:
    """The merge or move's record, re-read under both fleet rows' locks; refused when
    it cannot be undone."""
    row = await conn.fetchrow(GET_MERGE, merge_id)
    if row is None:
        raise UnknownFleetMerge(f"unknown fleet merge: {merge_id}")
    record = row_to_merge(row)
    src, tgt = record.source_fleet_id, record.target_fleet_id
    locked = {str(r["id"]): r for r in await conn.fetch(LOCK_FLEETS, [src, tgt])}
    involving = [row_to_merge(r) for r in await conn.fetch(MERGES_INVOLVING, [src, tgt])]
    # Re-read under the locks: a racing merge or undo has finished by now.
    record = next((m for m in involving if m.id == merge_id), record)
    reason = undo_refusal(
        record,
        involving,
        row_to_fleet(locked[tgt]) if tgt in locked else None,
        row_to_fleet(locked[src]) if src in locked else None,
    )
    if reason is not None:
        raise (FleetMoveRefused if record.kind == KIND_MOVE else FleetMergeRefused)(reason)
    return record


class _PinsBack(NamedTuple):
    """What an undo did with pins: how many returned, and the lists it reports."""

    returned_count: int
    restored: list[DroppedPin]
    skipped: list[DroppedPin]
    unpinned: list[DroppedPin]


async def _return_moved_pins(
    conn: asyncpg.Connection, record: FleetMergeRecord, back: list[str]
) -> _PinsBack:
    """Undo of a move: the pins on the moved entries go home to the source, and the
    source's dropped pins are re-pinned, both under the source's cap."""
    src, tgt = record.source_fleet_id, record.target_fleet_id
    restored: list[DroppedPin] = []
    skipped: list[DroppedPin] = []
    unpinned: list[DroppedPin] = []
    # A move's pins that come back face the source's cap like any
    # restored pin (the source may have refilled its briefing).
    returning = [
        DroppedPin(str(r["fleet_id"]), str(r["entry_id"]), r["pinned_by"], r["pinned_at"])
        for r in await conn.fetch(MOVE_RETURNING_PINS, tgt, back)
    ]
    mine = [d for d in record.dropped_pins if d.fleet_id == src]
    held = await conn.fetch(PINNED_ENTRY_IDS, src)
    pinned = {str(r["entry_id"]) for r in held}
    home = {
        str(r["id"]) for r in await conn.fetch(ENTRIES_IN_FLEET, [d.entry_id for d in mine], src)
    }
    back_pinned = {d.entry_id for d in returning}  # already coming back
    fits = [
        d
        for d in mine
        if d.entry_id in home and d.entry_id not in pinned and d.entry_id not in back_pinned
    ]
    skipped.extend(d for d in mine if d not in fits and d.entry_id not in back_pinned)
    put, over = plan_pin_restore([*returning, *fits], [r["pinned_by"] for r in held])
    for d in put:
        if d in returning:
            await conn.execute(MOVE_PIN_HOME, tgt, src, d.entry_id)
        else:
            await conn.execute(RESTORE_PIN, src, d.entry_id, d.pinned_by, d.pinned_at)
            restored.append(d)
    for d in over:
        if d in returning:
            await conn.execute(DELETE_PINS_OF, tgt, [d.entry_id])
            unpinned.append(d)
        skipped.append(d)
    return _PinsBack(sum(1 for d in put if d in returning), restored, skipped, unpinned)


async def _restore_dropped_pins(
    conn: asyncpg.Connection, record: FleetMergeRecord, pins: _PinsBack
) -> None:
    """Re-pin the pins the merge or move dropped, fleet by fleet, into ``pins``."""
    src, tgt = record.source_fleet_id, record.target_fleet_id
    for fid in (tgt,) if record.kind == KIND_MOVE else (src, tgt):
        mine = [d for d in record.dropped_pins if d.fleet_id == fid]
        if not mine:
            continue
        put, left = await restore_fleet_pins(conn, fid, mine, ENTRIES_IN_FLEET)
        pins.restored.extend(put)
        pins.skipped.extend(left)


class FleetMergeStore(PgConnections):
    async def duplicate_pairs(
        self, fleet_id: str, other_id: str, threshold: float, cap: int
    ) -> DuplicateScan:
        if not (is_valid_uuid(fleet_id) and is_valid_uuid(other_id)):
            return DuplicateScan()
        total = 0
        # One nearest-neighbour lookup per entry: bounded in time (the pool's
        # command timeout would cancel it anyway). When the server cancels the
        # scan, try a tenth of the entries once, then report none looked up.
        for looked in (cap, max(cap // 10, 1)):
            try:
                async with self._transaction() as conn:
                    await conn.execute(VECTOR_SEARCH_SETTINGS)
                    await conn.execute(DUPLICATE_SCAN_SETTINGS)
                    total = int(await conn.fetchval(COUNT_DUPLICATE_CANDIDATES, fleet_id))
                    # A small fleet looked into is scanned exactly (no fleet_id
                    # index, so the HNSW scan would be filtered; ADR 0126).
                    into = int(
                        await conn.fetchval(
                            COUNT_DUPLICATE_CANDIDATES_UPTO, other_id, EXACT_DUPLICATE_LOOKUP_MAX
                        )
                    )
                    sql = (
                        DUPLICATE_PAIRS_EXACT
                        if into <= EXACT_DUPLICATE_LOOKUP_MAX
                        else DUPLICATE_PAIRS
                    )
                    rows = await conn.fetch(sql, fleet_id, other_id, looked, threshold)
            except asyncpg.exceptions.QueryCanceledError:
                continue
            pairs = tuple(
                DuplicatePair(str(r["entry_id"]), str(r["match_id"]), float(r["sim"])) for r in rows
            )
            return DuplicateScan(pairs, min(total, looked), total > looked)
        return DuplicateScan((), 0, True)

    async def merge_fleets(
        self,
        source_id: str,
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
        merged_by: str,
    ) -> MergeOutcome:
        _require_one_target(target_fleet_id, new_fleet_name)
        source_id, target_fleet_id = _canonical_fleet_ids(
            source_id, target_fleet_id, FleetMergeRefused("a fleet cannot be merged into itself")
        )
        with _fleets_contention(FleetMergeRefused, "merge"):
            async with self._transaction() as conn:
                # Give up rather than queue behind in-flight writes (the migrations' pattern).
                await conn.execute(MERGE_SETTINGS)
                created = target_fleet_id is None
                if target_fleet_id is None:
                    target_fleet_id = await _create_target_fleet(conn, source_id, new_fleet_name)
                locked = await _lock_unmerged_fleets(conn, source_id, target_fleet_id)
                pins = await _lock_and_load_pins(conn, source_id, target_fleet_id)
                check_pins_unchanged(pins, expected_pin_ids)
                kept, dropped = choose_pins(pins, keep_pin_ids)

                merge_id = str(uuid.uuid4())
                moved = await _move_all_entries(conn, merge_id, source_id, target_fleet_id)
                agents = sorted(
                    r["name"] for r in await conn.fetch(MOVE_AGENTS, source_id, target_fleet_id)
                )
                await _delete_dropped_pins(conn, dropped, source_id, target_fleet_id)
                merged_at = await _fold_source_into_target(conn, source_id, target_fleet_id)
                drops = _dropped_pins(dropped)
                await _record_merge(
                    conn,
                    merge_id=merge_id,
                    source_id=source_id,
                    target_fleet_id=target_fleet_id,
                    done_by=merged_by,
                    done_at=merged_at,
                    agents=agents,
                    drops=drops,
                    kind=KIND_MERGE,
                )
                target_row = locked[target_fleet_id]
        record = FleetMergeRecord(
            merge_id, source_id, target_fleet_id, merged_by, merged_at, tuple(agents), drops
        )
        target = row_to_fleet(target_row)
        return MergeOutcome(
            record,
            target,
            created,
            len(moved),
            len(agents),
            tuple(replace(p, fleet_id=target_fleet_id) for p in kept),
            drops,
        )

    async def plan_fleet_move(self, source_id: str, agents: Sequence[str]) -> MovePlan:
        if not is_valid_uuid(source_id):
            return MovePlan()
        async with self._connection() as conn:
            rows = await conn.fetch(
                PLAN_MOVE, source_id, sorted(set(agents)), timeout=LONG_OP_TIMEOUT
            )
            total = int(
                await conn.fetchval(
                    COUNT_MOVER_ENTRIES, source_id, sorted(set(agents)), timeout=LONG_OP_TIMEOUT
                )
            )
        states: dict[str, int] = {}
        for r in rows:
            states[r["state"]] = states.get(r["state"], 0) + 1
        return MovePlan(tuple(str(r["id"]) for r in rows), states, total - len(rows))

    async def move_agents(
        self,
        source_id: str,
        agents: Sequence[str],
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
        moved_by: str,
    ) -> MoveOutcome:
        _require_one_target(target_fleet_id, new_fleet_name)
        names = sorted(set(agents))
        if not names:
            raise InvalidInput("name at least one agent to move")
        source_id, target_fleet_id = _canonical_fleet_ids(
            source_id,
            target_fleet_id,
            FleetMoveRefused("agents cannot be moved into the fleet they are in"),
        )
        with _fleets_contention(FleetMoveRefused, "move"):
            async with self._transaction() as conn:
                await conn.execute(MERGE_SETTINGS)
                created = target_fleet_id is None
                if target_fleet_id is None:
                    target_fleet_id = await _create_target_fleet(conn, source_id, new_fleet_name)
                # No entry can land in the source after the list below: a fleet write waits.
                locked = await _lock_unmerged_fleets(conn, source_id, target_fleet_id)
                await _lock_source_agents(conn, names, source_id, locked[source_id]["name"])
                await _lock_fleet_pins(conn, source_id, target_fleet_id)
                moving, held = await _lock_and_plan_movers(conn, source_id, names)
                pins = [
                    row_to_pin(r)
                    for r in await conn.fetch(
                        MOVE_PIN_CANDIDATES, source_id, target_fleet_id, moving
                    )
                ]
                check_pins_unchanged(pins, expected_pin_ids, FleetMoveRefused)
                kept, dropped = choose_pins(pins, keep_pin_ids)

                move_id = str(uuid.uuid4())
                await _move_these_entries(conn, move_id, source_id, target_fleet_id, moving)
                await _delete_dropped_pins(conn, dropped, source_id, target_fleet_id)
                await conn.execute(REKEY_THESE_PINS, source_id, target_fleet_id, moving)
                await conn.fetch(MOVE_THESE_AGENTS, names, target_fleet_id, source_id)
                moved_at = await conn.fetchval("SELECT clock_timestamp()")
                drops = _dropped_pins(dropped)
                await _record_merge(
                    conn,
                    merge_id=move_id,
                    source_id=source_id,
                    target_fleet_id=target_fleet_id,
                    done_by=moved_by,
                    done_at=moved_at,
                    agents=names,
                    drops=drops,
                    kind=KIND_MOVE,
                )
                target_row = locked[target_fleet_id]
        record = FleetMergeRecord(
            move_id,
            source_id,
            target_fleet_id,
            moved_by,
            moved_at,
            tuple(names),
            drops,
            kind=KIND_MOVE,
        )
        return MoveOutcome(
            record,
            row_to_fleet(target_row),
            created,
            len(moving),
            held,
            len(names),
            tuple(replace(p, fleet_id=target_fleet_id) for p in kept),
            drops,
        )

    async def unmerge_fleet(self, merge_id: str) -> UnmergeOutcome:
        if not is_valid_uuid(merge_id):
            raise UnknownFleetMerge(f"unknown fleet merge: {merge_id}")
        merge_id = str(uuid.UUID(merge_id))
        with _fleets_contention(FleetMergeRefused, "undo"):
            async with self._transaction() as conn:
                await conn.execute(MERGE_SETTINGS)
                record = await _load_undoable_merge(conn, merge_id)
                src, tgt = record.source_fleet_id, record.target_fleet_id
                await _lock_fleet_pins(conn, src, tgt)
                back = [
                    str(r["id"])
                    for r in await conn.fetch(
                        RESTORE_ENTRIES, merge_id, src, tgt, timeout=LONG_OP_TIMEOUT
                    )
                ]
                agents = sorted(
                    r["name"]
                    for r in await conn.fetch(RESTORE_AGENTS, list(record.moved_agents), src, tgt)
                )
                if record.kind == KIND_MOVE:
                    pins = await _return_moved_pins(conn, record, back)
                else:
                    returned = len(await conn.fetch(RETURN_PINS, merge_id, src, tgt))
                    pins = _PinsBack(returned, [], [], [])
                await _restore_dropped_pins(conn, record, pins)
                if record.kind != KIND_MOVE:  # a move never marked its source
                    await conn.execute(CLEAR_MERGED, src)
                undone_at = await conn.fetchval(MARK_UNDONE, merge_id)
        return UnmergeOutcome(
            replace(record, undone_at=undone_at),
            len(back),
            tuple(agents),
            pins.returned_count,
            tuple(pins.restored),
            tuple(pins.skipped),
            tuple(pins.unpinned),
        )

    async def list_fleet_merges(self, limit: int) -> list[FleetMergeRecord]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_MERGES, limit)
        return [row_to_merge(r) for r in rows]

    async def get_fleet_merge(self, merge_id: str) -> FleetMergeRecord | None:
        if not is_valid_uuid(merge_id):
            return None
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_MERGE, merge_id)
        return row_to_merge(row) if row is not None else None
