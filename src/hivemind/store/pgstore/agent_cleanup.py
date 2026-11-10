"""Agent cleanups and their undo."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import datetime

from hivemind.domain.access import Visibility
from hivemind.domain.agent_cleanup import (
    PREVIEW_RESTORE_LIMIT,
    PREVIEW_WITHDRAW_LIMIT,
    AgentCleanupRecord,
    AgentCleanupRefused,
    CleanupOutcome,
    CleanupScan,
    CleanupUndoOutcome,
    RestoredEntry,
    UnknownAgentCleanup,
    check_counts_unchanged,
    check_restore_ids,
    check_window,
    cleanup_reason,
)
from hivemind.domain.agent_cleanup import undo_refusal as cleanup_undo_refusal
from hivemind.domain.fleet_merge import DroppedPin
from hivemind.domain.validation import is_valid_uuid
from hivemind.store.pgstore.connection import (
    LONG_OP_TIMEOUT,
    MERGE_SETTINGS,
    PgConnections,
    refuse_on_contention,
)
from hivemind.store.pgstore.pin_common import LOCK_FLEET_PINS, restore_fleet_pins
from hivemind.store.pgstore.rows import dropped_json, row_to_cleanup, row_to_entry
from hivemind.store.pgstore.sql_common import _ENTRY_COLUMNS, readable_clause

# The memories a cleanup withdraws: active, authored by one of $1 (a text[]:
# the agent, or every author it owns, ADR 0118), written in the window
# [$2, $3) (``created_at``: write time, not the memory date). The rating
# statements below take $1 as the one agent whose ratings they are.
_CLEANUP_WITHDRAW_WHERE = (
    "author = ANY($1::text[]) AND state = 'active' AND created_at >= $2 AND created_at < $3"
)

CLEANUP_WITHDRAW_LIST = (
    "SELECT "
    + _ENTRY_COLUMNS
    + " FROM entries WHERE "
    + _CLEANUP_WITHDRAW_WHERE
    + " ORDER BY created_at DESC, id DESC LIMIT $4"
)

CLEANUP_WITHDRAW_COUNT = "SELECT count(*) FROM entries WHERE " + _CLEANUP_WITHDRAW_WHERE

CLEANUP_WITHDRAW_LOCK = (
    "SELECT id, fleet_id FROM entries WHERE " + _CLEANUP_WITHDRAW_WHERE + " ORDER BY id FOR UPDATE"
)

CLEANUP_FLEETS = (
    "SELECT DISTINCT fleet_id FROM entries WHERE "
    + _CLEANUP_WITHDRAW_WHERE
    + " AND fleet_id IS NOT NULL"
)

# The ratings a cleanup sets aside: the agent's, last written in the window,
# and not already set aside by an earlier cleanup.
_CLEANUP_RATINGS_WHERE = (
    'f."user" = $1 AND f.updated_at >= $2 AND f.updated_at < $3 AND f.set_aside_by IS NULL'
)

CLEANUP_RATINGS_BY_VERDICT = (
    "SELECT f.verdict, count(*) AS n FROM feedbacks f WHERE "
    + _CLEANUP_RATINGS_WHERE
    + " GROUP BY f.verdict"
)

CLEANUP_SET_ASIDE = (
    "UPDATE feedbacks f SET set_aside_by = $4 WHERE " + _CLEANUP_RATINGS_WHERE + " RETURNING 1"
)

# The agent's own memories written in the window, in any state ($1 agent, $2
# since, $3 until).
_CLEANUP_OWN = "SELECT id, state, superseded_by FROM entries WHERE author = ANY($1::text[]) AND created_at >= $2 AND created_at < $3"


# Memories the agent's memories replaced (ADR 0111): superseded, not the
# agent's own window memories, and the ``superseded_by`` chain from them passes
# only through the agent's own window memories and ends at one that is still
# active (a memory the cleanup withdraws). ``first`` is the memory that
# replaced it, ``head`` the end of the chain. Nobody else's memory is in the
# chain, so a replacement made by another agent since is never undone.
def _restorable_ctes(readable: str | None = None) -> str:
    """The CTEs this describes. ``readable`` is an extra condition on a candidate ``e``
    (a visibility clause, ADR 0118): memories the reader may not read are
    never offered, so never restored."""
    extra = f" AND {readable}" if readable else ""
    return f"""
own AS ({_CLEANUP_OWN}),
chain(root, first, cur) AS (
    SELECT e.id, e.superseded_by, e.superseded_by FROM entries e
     WHERE e.state = 'superseded' AND e.superseded_by IN (SELECT id FROM own)
       AND e.id NOT IN (SELECT id FROM own) AND e.redacted_at IS NULL{extra}
    UNION
    SELECT c.root, c.first, o.superseded_by FROM chain c JOIN own o ON o.id = c.cur
     WHERE o.state = 'superseded' AND o.superseded_by IN (SELECT id FROM own)
),
restorable AS (
    SELECT c.root AS id, c.first, c.cur AS head
      FROM chain c JOIN own h ON h.id = c.cur AND h.state = 'active'
)
"""


def _cleanup_restorable_list(readable: str | None = None) -> str:
    return (
        "WITH RECURSIVE "
        + _restorable_ctes(readable)
        + "SELECT "
        + _ENTRY_COLUMNS
        + " FROM entries WHERE id IN (SELECT id FROM restorable)"
        " ORDER BY created_at DESC, id DESC LIMIT $4"
    )


def _cleanup_restorable_count(readable: str | None = None) -> str:
    return "WITH RECURSIVE " + _restorable_ctes(readable) + "SELECT count(*) FROM restorable"


CLEANUP_AGENT_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('hivemind.cleanup:' || $1, 0))"

CLEANUP_WITHDRAW = """
UPDATE entries SET state = 'withdrawn', withdrawn_reason = $2, withdrawn_at = now()
 WHERE id = ANY($1::uuid[]) AND state = 'active'
"""


def _cleanup_restorable_lock(readable: str | None = None) -> str:
    return (
        "WITH RECURSIVE "
        + _restorable_ctes(readable)
        + "SELECT e.id, r.first, r.head FROM entries e JOIN restorable r ON r.id = e.id"
        " ORDER BY e.id FOR UPDATE OF e"
    )


CLEANUP_RESTORE = """
UPDATE entries SET state = 'active', superseded_by = NULL
 WHERE id = ANY($1::uuid[]) AND state = 'superseded' AND redacted_at IS NULL
"""


# A pin shows the end of its entry's supersession chain, so a pin on an older
# version of a withdrawn memory comes off with it (as ``PinService`` does for
# a withdrawal), except on the memories being restored.
def _pin_walk(stop: str) -> str:
    """The recursive ``walk(pinned, cur)``: where each pin's entry leads along
    ``superseded_by``. ``stop`` is a condition on ``w.cur`` that ends the walk,
    so a pin on an older version of a memory being restored is not followed
    past it (it shows that restored memory, which stays)."""
    return (
        "walk(pinned, cur) AS ("
        " SELECT entry_id, entry_id FROM pins"
        " UNION"
        " SELECT w.pinned, e.superseded_by FROM walk w JOIN entries e ON e.id = w.cur"
        " WHERE e.state = 'superseded' AND e.superseded_by IS NOT NULL AND " + stop + ") "
    )


def _cleanup_pins_count(readable: str | None = None) -> str:
    return (
        "WITH RECURSIVE "
        + _restorable_ctes(readable)
        + ", "
        + _pin_walk("w.cur NOT IN (SELECT id FROM restorable)")
        + "SELECT count(*) FROM pins WHERE entry_id IN ("
        "SELECT pinned FROM walk WHERE cur IN (SELECT id FROM own WHERE state = 'active'))"
        " AND entry_id NOT IN (SELECT id FROM restorable)"
    )


# The fleets of every pin a cleanup of the window could remove (supersession
# can cross fleets, e.g. an admin's org successor), locked before the entries.
CLEANUP_PIN_FLEETS = (
    "WITH RECURSIVE "
    + _pin_walk("TRUE")
    + "SELECT DISTINCT p.fleet_id FROM pins p WHERE p.entry_id IN ("
    "SELECT pinned FROM walk WHERE cur IN (SELECT id FROM entries WHERE "
    + _CLEANUP_WITHDRAW_WHERE
    + "))"
)

CLEANUP_UNPIN = (
    "WITH RECURSIVE "
    + _pin_walk("NOT (w.cur = ANY($2::uuid[]))")
    + "DELETE FROM pins WHERE entry_id IN (SELECT pinned FROM walk WHERE cur = ANY($1::uuid[]))"
    " AND NOT (entry_id = ANY($2::uuid[]))"
    " RETURNING fleet_id, entry_id, pinned_by, pinned_at"
)

CLEANUP_INSERT = """
INSERT INTO agent_cleanups (id, agent, since, until, cleaned_by, cleaned_at, withdrawn_ids,
                            restored, ratings_set_aside, unpinned_pins, self_service)
VALUES ($1, $2, $3, $4, $5, clock_timestamp(), $6, $7, $8, $9, $10)
RETURNING cleaned_at
"""

_CLEANUP_COLUMNS = (
    "id, agent, since, until, cleaned_by, cleaned_at, withdrawn_ids, restored, "
    "ratings_set_aside, unpinned_pins, revoked, undone_at, self_service"
)

CLEANUP_GET = f"SELECT {_CLEANUP_COLUMNS} FROM agent_cleanups WHERE id = $1"

CLEANUP_LIST = (
    f"SELECT {_CLEANUP_COLUMNS} FROM agent_cleanups ORDER BY cleaned_at DESC, id DESC LIMIT $1"
)

CLEANUP_OF_AGENT = (
    f"SELECT {_CLEANUP_COLUMNS} FROM agent_cleanups WHERE agent = $1"
    " ORDER BY cleaned_at DESC, id DESC"
)

CLEANUP_MARK_REVOKED = "UPDATE agent_cleanups SET revoked = true WHERE id = $1"

CLEANUP_MARK_UNDONE = (
    "UPDATE agent_cleanups SET undone_at = clock_timestamp() WHERE id = $1 RETURNING undone_at"
)

CLEANUP_FLEETS_OF = (
    "SELECT DISTINCT fleet_id FROM entries WHERE id = ANY($1::uuid[]) AND fleet_id IS NOT NULL"
)

CLEANUP_AUTHORS_OF = "SELECT DISTINCT author FROM entries WHERE id = ANY($1::uuid[])"

CLEANUP_LOCK_ENTRIES = "SELECT id FROM entries WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE"

CLEANUP_REACTIVATE = """
UPDATE entries SET state = 'active', withdrawn_reason = NULL, withdrawn_at = NULL
 WHERE id = ANY($1::uuid[]) AND state = 'withdrawn' AND withdrawn_reason = $2
   AND redacted_at IS NULL
RETURNING id
"""

CLEANUP_RESUPERSEDE = """
UPDATE entries e SET state = 'superseded', superseded_by = r.successor
  FROM unnest($1::uuid[], $2::uuid[], $3::uuid[]) AS r(id, successor, head)
 WHERE e.id = r.id AND e.state = 'active'
   AND EXISTS (SELECT 1 FROM entries s WHERE s.id = r.head AND s.state = 'active')
RETURNING e.id
"""

CLEANUP_CLEAR_SET_ASIDE = (
    "UPDATE feedbacks SET set_aside_by = NULL WHERE set_aside_by = $1 RETURNING 1"
)

CLEANUP_PINNABLE = (
    "SELECT id FROM entries WHERE id = ANY($1::uuid[]) AND fleet_id = $2"
    " AND state IN ('active', 'superseded')"
)


def _cleanup_contention(verb: str) -> AbstractContextManager[None]:
    """``refuse_on_contention`` with the agent cleanup and undo messages."""
    return refuse_on_contention(
        AgentCleanupRefused,
        "the agent's memories are busy with writes or another cleanup; try again in a moment",
        f"the {verb} deadlocked with a concurrent change; try it again",
    )


class AgentCleanupStore(PgConnections):
    async def scan_agent_cleanup(
        self,
        agent: str,
        since: datetime,
        until: datetime,
        *,
        authors: Sequence[str] | None = None,
        visibility: Visibility | None = None,
    ) -> CleanupScan:
        names = sorted(set(authors or ()) | {agent})
        # The restorable statements bind $1 to $3 (list: $4 too); the
        # visibility parameters follow.
        vis3, extra3 = readable_clause(visibility, 3)
        vis4, extra4 = readable_clause(visibility, 4)
        # One snapshot, so the totals, the lists and the pin count agree.
        async with (
            self._connection() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            withdraw = await conn.fetch(
                CLEANUP_WITHDRAW_LIST, names, since, until, PREVIEW_WITHDRAW_LIMIT
            )
            withdraw_total = int(await conn.fetchval(CLEANUP_WITHDRAW_COUNT, names, since, until))
            verdicts = await conn.fetch(CLEANUP_RATINGS_BY_VERDICT, agent, since, until)
            pins_total = int(
                await conn.fetchval(
                    _cleanup_pins_count(vis3),
                    names,
                    since,
                    until,
                    *extra3,
                    timeout=LONG_OP_TIMEOUT,
                )
            )
            restorable = await conn.fetch(
                _cleanup_restorable_list(vis4),
                names,
                since,
                until,
                PREVIEW_RESTORE_LIMIT,
                *extra4,
                timeout=LONG_OP_TIMEOUT,
            )
            restorable_total = int(
                await conn.fetchval(
                    _cleanup_restorable_count(vis3),
                    names,
                    since,
                    until,
                    *extra3,
                    timeout=LONG_OP_TIMEOUT,
                )
            )
        by_verdict = {r["verdict"]: int(r["n"]) for r in verdicts}
        return CleanupScan(
            since=since,
            until=until,
            withdraw=tuple(row_to_entry(r) for r in withdraw),
            withdraw_total=withdraw_total,
            ratings_total=sum(by_verdict.values()),
            ratings_by_verdict=by_verdict,
            pins_total=pins_total,
            restorable=tuple(row_to_entry(r) for r in restorable),
            restorable_total=restorable_total,
        )

    async def cleanup_agent(
        self,
        agent: str,
        since: datetime,
        until: datetime,
        *,
        restore_ids: Sequence[str] | None = None,
        expected_withdraw: int | None = None,
        expected_ratings: int | None = None,
        cleaned_by: str,
        authors: Sequence[str] | None = None,
        self_service: bool = False,
        visibility: Visibility | None = None,
    ) -> CleanupOutcome:
        check_window(since, until)
        names = sorted(set(authors or ()) | {agent})
        vis3, extra3 = readable_clause(visibility, 3)
        wanted = list(dict.fromkeys(restore_ids or ()))
        if any(not is_valid_uuid(w) for w in wanted):
            raise AgentCleanupRefused("restore_ids names memories that cannot be restored")
        wanted = [str(uuid.UUID(w)) for w in wanted]
        cleanup_id = str(uuid.uuid4())
        with _cleanup_contention("cleanup"):
            async with self._transaction() as conn:
                await conn.execute(MERGE_SETTINGS)
                # One cleanup or undo per agent at a time. A cleanup of several
                # owned authors (ADR 0118) takes each one's lock, in name order,
                # so it also waits for an admin cleanup of a retired agent.
                for name in names:
                    await conn.execute(CLEANUP_AGENT_LOCK, name)
                # Pin locks first, in fleet order (a merge takes the fleet rows,
                # then these), then the memory rows: a concurrent supersede or
                # withdraw of one waits for this commit, then sees its new state.
                fleets = sorted(
                    {
                        str(r["fleet_id"])
                        for r in await conn.fetch(CLEANUP_FLEETS, names, since, until)
                    }
                    | {
                        str(r["fleet_id"])
                        for r in await conn.fetch(
                            CLEANUP_PIN_FLEETS, names, since, until, timeout=LONG_OP_TIMEOUT
                        )
                    }
                )
                for fid in fleets:
                    await conn.execute(LOCK_FLEET_PINS, fid)
                locked = await conn.fetch(
                    CLEANUP_WITHDRAW_LOCK, names, since, until, timeout=LONG_OP_TIMEOUT
                )
                withdraw_ids = [str(r["id"]) for r in locked]
                restorable = {
                    str(r["id"]): (str(r["first"]), str(r["head"]))
                    for r in await conn.fetch(
                        _cleanup_restorable_lock(vis3),
                        names,
                        since,
                        until,
                        *extra3,
                        timeout=LONG_OP_TIMEOUT,
                    )
                }
                restore = check_restore_ids(list(restorable), wanted)
                # The agent's own upserts of these rows wait for the commit and
                # then clear the mark (UPSERT_FEEDBACK).
                set_aside = len(
                    await conn.fetch(
                        CLEANUP_SET_ASIDE, agent, since, until, cleanup_id, timeout=LONG_OP_TIMEOUT
                    )
                )
                check_counts_unchanged(
                    len(withdraw_ids), set_aside, expected_withdraw, expected_ratings
                )
                if not withdraw_ids and not set_aside:
                    return CleanupOutcome(None)
                unpinned: tuple[DroppedPin, ...] = ()
                if withdraw_ids:
                    await conn.execute(
                        CLEANUP_WITHDRAW,
                        withdraw_ids,
                        cleanup_reason(cleanup_id),
                        timeout=LONG_OP_TIMEOUT,
                    )
                    unpinned = tuple(
                        DroppedPin(
                            r["fleet_id"], str(r["entry_id"]), r["pinned_by"], r["pinned_at"]
                        )
                        for r in await conn.fetch(
                            CLEANUP_UNPIN, withdraw_ids, restore, timeout=LONG_OP_TIMEOUT
                        )
                    )
                if restore:
                    await conn.execute(CLEANUP_RESTORE, restore, timeout=LONG_OP_TIMEOUT)
                restored = tuple(RestoredEntry(e, *restorable[e]) for e in restore)
                cleaned_at = await conn.fetchval(
                    CLEANUP_INSERT,
                    cleanup_id,
                    agent,
                    since,
                    until,
                    cleaned_by,
                    withdraw_ids,
                    json.dumps(
                        [
                            {
                                "entry_id": r.entry_id,
                                "superseded_by": r.superseded_by,
                                "head": r.head,
                            }
                            for r in restored
                        ]
                    ),
                    set_aside,
                    dropped_json(unpinned),
                    self_service,
                )
        record = AgentCleanupRecord(
            cleanup_id,
            agent,
            since,
            until,
            cleaned_by,
            cleaned_at,
            tuple(withdraw_ids),
            restored,
            set_aside,
            unpinned,
            self_service=self_service,
        )
        return CleanupOutcome(record, len(withdraw_ids), set_aside, len(restored), unpinned)

    async def mark_cleanup_revoked(self, cleanup_id: str) -> None:
        if is_valid_uuid(cleanup_id):
            async with self._connection() as conn:
                await conn.execute(CLEANUP_MARK_REVOKED, cleanup_id)

    async def undo_agent_cleanup(self, cleanup_id: str) -> CleanupUndoOutcome:
        if not is_valid_uuid(cleanup_id):
            raise UnknownAgentCleanup(f"unknown agent cleanup: {cleanup_id}")
        cleanup_id = str(uuid.UUID(cleanup_id))
        with _cleanup_contention("undo"):
            async with self._transaction() as conn:
                await conn.execute(MERGE_SETTINGS)
                row = await conn.fetchrow(CLEANUP_GET, cleanup_id)
                if row is None:
                    raise UnknownAgentCleanup(f"unknown agent cleanup: {cleanup_id}")
                # The agent's lock, and that of every author whose memories it
                # withdrew, in name order as the apply takes them.
                authors = {
                    r["author"]
                    for r in await conn.fetch(CLEANUP_AUTHORS_OF, list(row["withdrawn_ids"]))
                }
                for name in sorted(authors | {row["agent"]}):
                    await conn.execute(CLEANUP_AGENT_LOCK, name)
                # Re-read under the lock: a racing cleanup or undo has finished by now.
                mine = [row_to_cleanup(r) for r in await conn.fetch(CLEANUP_OF_AGENT, row["agent"])]
                record = next(m for m in mine if m.id == cleanup_id)
                reason = cleanup_undo_refusal(record, mine)
                if reason is not None:
                    raise AgentCleanupRefused(reason)
                touched = list(
                    dict.fromkeys(
                        [
                            *record.withdrawn_ids,
                            *(r.entry_id for r in record.restored),
                            *(r.superseded_by for r in record.restored),
                            *(r.head for r in record.restored if r.head),
                        ]
                    )
                )
                fleets = sorted(
                    {str(r["fleet_id"]) for r in await conn.fetch(CLEANUP_FLEETS_OF, touched)}
                    | {d.fleet_id for d in record.unpinned_pins}
                )
                for fid in fleets:
                    await conn.execute(LOCK_FLEET_PINS, fid)
                await conn.execute(CLEANUP_LOCK_ENTRIES, touched, timeout=LONG_OP_TIMEOUT)
                back = await conn.fetch(
                    CLEANUP_REACTIVATE,
                    list(record.withdrawn_ids),
                    cleanup_reason(cleanup_id),
                    timeout=LONG_OP_TIMEOUT,
                )
                again = await conn.fetch(
                    CLEANUP_RESUPERSEDE,
                    [r.entry_id for r in record.restored],
                    [r.superseded_by for r in record.restored],
                    [r.head or r.superseded_by for r in record.restored],
                    timeout=LONG_OP_TIMEOUT,
                )
                cleared = await conn.fetch(
                    CLEANUP_CLEAR_SET_ASIDE, cleanup_id, timeout=LONG_OP_TIMEOUT
                )
                restored_pins: list[DroppedPin] = []
                skipped_pins: list[DroppedPin] = []
                for fid in sorted({d.fleet_id for d in record.unpinned_pins}):
                    mine_pins = [d for d in record.unpinned_pins if d.fleet_id == fid]
                    put, left = await restore_fleet_pins(conn, fid, mine_pins, CLEANUP_PINNABLE)
                    restored_pins.extend(put)
                    skipped_pins.extend(left)
                undone_at = await conn.fetchval(CLEANUP_MARK_UNDONE, cleanup_id)
        return CleanupUndoOutcome(
            replace(record, undone_at=undone_at),
            reactivated=len(back),
            reactivate_skipped=len(record.withdrawn_ids) - len(back),
            resuperseded=len(again),
            resupersede_skipped=len(record.restored) - len(again),
            ratings_cleared=len(cleared),
            pins_restored=tuple(restored_pins),
            pins_skipped=tuple(skipped_pins),
        )

    async def list_agent_cleanups(
        self, limit: int, *, agent: str | None = None
    ) -> list[AgentCleanupRecord]:
        async with self._connection() as conn:
            if agent is None:
                rows = await conn.fetch(CLEANUP_LIST, limit)
            else:
                rows = await conn.fetch(CLEANUP_OF_AGENT + " LIMIT $2", agent, limit)
        return [row_to_cleanup(r) for r in rows]

    async def get_agent_cleanup(self, cleanup_id: str) -> AgentCleanupRecord | None:
        if not is_valid_uuid(cleanup_id):
            return None
        async with self._connection() as conn:
            row = await conn.fetchrow(CLEANUP_GET, cleanup_id)
        return row_to_cleanup(row) if row is not None else None
