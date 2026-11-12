"""Entries: create, read, links, list, withdraw, restore and redact."""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

import asyncpg

from hivemind.domain.access import SCOPE_FLEET, Visibility
from hivemind.domain.entry import Entry, EntryDraft, EntryFilters, EntryState, ExtractedEntity
from hivemind.domain.entry_restore import Restoration, chain_conflict_message, restore_refusal
from hivemind.domain.redaction import (
    REDACTED_SUMMARY,
    RedactOutcome,
    RedactRefused,
    is_redaction_stub,
)
from hivemind.domain.upstream import UpstreamLink
from hivemind.domain.validation import canonical_uuid, is_valid_uuid
from hivemind.ports import SupersedeConflict
from hivemind.store.inheritance import INHERITED_FROM
from hivemind.store.pgstore.agent_cleanup import CLEANUP_AGENT_LOCK
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.fleet_guards import refuse_merged, refuse_stale_home
from hivemind.store.pgstore.rows import encode_entities, encode_sources, row_to_entry
from hivemind.store.pgstore.sql_common import (
    _ENTRY_COLUMNS,
    _RATING_COUNTS,
    filter_conditions,
    readable_clause,
    visibility_clause,
)
from hivemind.store.pgstore.uuids import valid_uuids

INSERT_ENTRY = """
INSERT INTO entries (
    id, kind, summary, body, payload, sources, tags,
    occurred_at, author, agent, importance, importance_source, scope, fleet_id,
    embedding, embedding_model,
    entities, entity_names, entities_model
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
    $16, $17, $18, $19
)
"""
# ``created_at`` takes the schema's ``now()`` default.

INSERT_ENTRY_RETURNING = INSERT_ENTRY.rstrip() + " RETURNING " + _ENTRY_COLUMNS

SELECT_ENTRY = "SELECT " + _ENTRY_COLUMNS + " FROM entries WHERE id = $1"

SELECT_ENTRIES = "SELECT " + _ENTRY_COLUMNS + " FROM entries WHERE id = ANY($1)"

# Reverse supersession link (PERF-1/STORE-2): served by the partial
# ``entries_superseded_by_idx`` (migration 0008); the predicate below must
# stay implied by the index's ``WHERE superseded_by IS NOT NULL``.
SELECT_PREDECESSORS = (
    "SELECT " + _ENTRY_COLUMNS + " FROM entries "
    "WHERE superseded_by = ANY($1) AND superseded_by IS NOT NULL"
)

# "See also" links (ADR 0057). The insert skips ids that name no entry
# (the join), and the reads are oldest link first.
INSERT_LINKS = """
INSERT INTO entry_links (from_id, to_id)
SELECT $1, e.id FROM entries e WHERE e.id = ANY($2::uuid[]) AND e.id <> $1
ON CONFLICT DO NOTHING
"""

SELECT_LINKS_OUT = "SELECT to_id FROM entry_links WHERE from_id = $1 ORDER BY created_at, to_id"

# Incoming links are cut to $2 after keeping only active sources the reader
# may see (``{vis}``, on ``e``), so entries it cannot read never use up the cap.
SELECT_LINKS_IN = """
SELECT l.from_id FROM entry_links l JOIN entries e ON e.id = l.from_id
 WHERE l.to_id = $1 AND e.state = 'active'{vis}
 ORDER BY l.created_at DESC, l.from_id LIMIT $2
"""

# The same reads for several entries at once (ADR 0059): one statement
# each, the incoming side still at most $2 links per entry.
SELECT_LINKS_OUT_MANY = """
SELECT from_id, to_id FROM entry_links WHERE from_id = ANY($1::uuid[])
 ORDER BY from_id, created_at, to_id
"""

SELECT_LINKS_IN_MANY = """
SELECT t.id AS to_id, l.from_id
  FROM unnest($1::uuid[]) AS t(id)
 CROSS JOIN LATERAL (
       SELECT k.from_id, k.created_at FROM entry_links k JOIN entries e ON e.id = k.from_id
        WHERE k.to_id = t.id AND e.state = 'active'{vis}
        ORDER BY k.created_at DESC, k.from_id LIMIT $2) AS l
 ORDER BY t.id, l.created_at DESC, l.from_id
"""

FLIP_SUPERSEDED = (
    """
UPDATE entries
SET state = 'superseded', superseded_by = $1
WHERE id = ANY($2) AND state = 'active'
  AND (scope <> 'fleet' OR $4 = 'org' OR fleet_id = $3::uuid)
  AND (scope <> 'self' OR $4 = 'org' OR author = $5 OR author = ANY("""
    + INHERITED_FROM.replace("$1", "$5")
    + """::text[]))
RETURNING id
"""
)
# A fleet target flips only for an org successor (the admin's, ADR 0060) or one
# written into its own fleet: a fleet move between the service's
# ``may_supersede`` check and this transaction may have changed the target's
# fleet (ADR 0033). A NULL ``$3`` matches no fleet. A `self` target flips only for
# an org successor, its author or an heir: a move to self may have narrowed it since the
# check (ADR 0145).

LOOKUP_STATE = "SELECT state FROM entries WHERE id = $1"

WITHDRAW = (
    "UPDATE entries SET state = 'withdrawn', withdrawn_reason = $2, withdrawn_at = now() "
    "WHERE id = $1 AND state = 'active' RETURNING " + _ENTRY_COLUMNS
)

# Restoring one withdrawn memory (ADR 0124). The row is locked first, so a
# concurrent withdraw or supersede of it waits and then sees the new state;
# the transaction advisory lock serialises two restores, whose chain checks
# would otherwise not see each other.
RESTORE_LOCK = "SELECT pg_advisory_xact_lock(hashtext('entry.restore'))"

# The author never changes, so it is read before any lock: the restore then takes
# that author's cleanup lock (``CLEANUP_AGENT_LOCK``) before the row, as a
# cleanup and its undo do, so a cleanup of the author never misses a memory a
# restore is putting back.
RESTORE_AUTHOR = "SELECT author FROM entries WHERE id = $1"

RESTORE_LOCK_ROW = (
    "SELECT state, withdrawn_reason, redacted_at, summary, body, "
    "embedding IS NOT NULL AS has_embedding FROM entries WHERE id = $1 FOR UPDATE"
)

# A not-undone cleanup that withdrew the memory (its reason is the cleanup's).
RESTORE_STANDING_CLEANUP = """
SELECT id FROM agent_cleanups
 WHERE undone_at IS NULL AND $1::uuid = ANY(withdrawn_ids)
   AND 'admin cleanup ' || id::text = $2
 LIMIT 1
"""

# The chain rules (ADR 0124). A cleanup that put the memory back cut its link to
# the memory that replaced it: any such record makes the restore refuse.
RESTORE_WAS_CUT = """
SELECT 1 FROM agent_cleanups c, jsonb_array_elements(c.restored) r
 WHERE r ->> 'entry_id' = $1::text LIMIT 1
"""

# An active memory that names it as its successor (never produced by the state
# rules), or an active memory a cleanup put back whose recorded successor chain
# leads to it.
RESTORE_LINKED_BY = (
    "SELECT id FROM entries WHERE superseded_by = $1::uuid AND state = 'active' LIMIT 1"
)

RESTORE_PUT_BACK_INSTEAD = """
WITH RECURSIVE walk(root, cur) AS (
    SELECT (r ->> 'entry_id')::uuid, (r ->> 'superseded_by')::uuid
      FROM agent_cleanups c, jsonb_array_elements(c.restored) r
      JOIN entries g ON g.id = (r ->> 'entry_id')::uuid AND g.state = 'active'
    UNION
    SELECT w.root, e.superseded_by FROM walk w JOIN entries e ON e.id = w.cur
     WHERE e.superseded_by IS NOT NULL
)
SELECT root FROM walk WHERE cur = $1::uuid LIMIT 1
"""

RESTORE = (
    "UPDATE entries SET state = 'active', withdrawn_reason = NULL, withdrawn_at = NULL "
    "WHERE id = $1 AND state = 'withdrawn' AND redacted_at IS NULL RETURNING " + _ENTRY_COLUMNS
)

# ADR 0125: wipe every free-text field of a withdrawn memory in one UPDATE (the
# keyword column is cleared by ``REDACT_CLEAR_TSV``: its trigger refills it on
# any write to ``summary``). Kind, scope, author, fleet, dates, importance,
# supersession and the feedback stay. Outgoing see-also links are derived from
# the text and go (``REDACT_DROP_LINKS``); links pointing at the memory stay, so
# the linking memories still learn their target changed.
REDACT_LOCK = "SELECT state, redacted_at FROM entries WHERE id = $1 FOR NO KEY UPDATE"

REDACT = (
    "UPDATE entries SET summary = $2, body = NULL, payload = NULL, sources = '[]'::jsonb, "
    "tags = '{}', entities = '[]'::jsonb, entity_names = '{}', entities_model = NULL, "
    "embedding = NULL, embedding_model = NULL, "
    "withdrawn_reason = CASE WHEN state = 'withdrawn' THEN $2 END, redacted_at = now() "
    "WHERE id = $1 AND state IN ('withdrawn', 'superseded') AND redacted_at IS NULL "
    "RETURNING " + _ENTRY_COLUMNS
)

REDACT_CLEAR_TSV = "UPDATE entries SET search_tsv = NULL WHERE id = $1"

# Audit rows of this memory that hold a withdrawal reason: an `entry.withdraw`
# row's `reason`, and the `previous_reason` an `entry.restore` row copied from
# the memory (ADR 0124). The audit log is never edited, so these are counted for
# the admin to review by hand.
REDACT_COUNT_WITHDRAW_REASONS = (
    "SELECT count(*) FROM audit_log WHERE target = $1::text AND ("
    "(action = 'entry.withdraw' AND btrim(coalesce(detail->>'reason', '')) <> '') "
    "OR (action = 'entry.restore' AND btrim(coalesce(detail->>'previous_reason', '')) <> '') "
    "OR (action = 'entry.move_to_self' AND btrim(coalesce(detail->>'reason', '')) <> ''))"
)

REDACT_DROP_LINKS = "DELETE FROM entry_links WHERE from_id = $1"

REDACT_COUNT_NOTES = (
    "SELECT count(*) AS notes, count(*) FILTER (WHERE verdict IN ('stale', 'wrong')) AS flags "
    "FROM feedbacks WHERE entry_id = $1 AND note IS NOT NULL AND btrim(note) <> ''"
)

# What happened to the targets of several entries' "see also" links,
# one statement. ``{vis}`` is the visibility clause on the target (columns
# resolve to ``entries`` inside the subquery; any state). The feedback dates
# are the two "last confirmed" is made of (BATCH_FEEDBACK_SIGNALS). A link
# is at most five per entry, served by the primary key (from_id, to_id).
SELECT_UPSTREAM_LINKS = f"""
SELECT l.from_id, l.to_id, l.created_at AS linked_at, t.state,
       s.created_at AS superseded_at, s.author AS superseded_author,
       fb.newest_flag_at, fb.newest_noted_helpful_at
  FROM entry_links l
  JOIN (SELECT id, state, superseded_by FROM entries
         WHERE id IN (SELECT to_id FROM entry_links WHERE from_id = ANY($1::uuid[])){{vis}}) t
    ON t.id = l.to_id
  LEFT JOIN entries s ON s.id = t.superseded_by
  LEFT JOIN LATERAL (
       SELECT MAX(f.updated_at) FILTER (WHERE f.verdict IN ('stale', 'wrong')) AS newest_flag_at,
              MAX(f.updated_at) FILTER (
                  WHERE f.verdict = 'helpful' AND coalesce(f.note, '') ~ '\\S'
              ) AS newest_noted_helpful_at
         FROM feedbacks f
        WHERE f.entry_id = t.id AND {_RATING_COUNTS}) fb ON true
 WHERE l.from_id = ANY($1::uuid[])
 ORDER BY l.from_id, l.created_at, l.to_id
"""


async def _insert_entry(
    conn: asyncpg.Connection,
    entry_id: str,
    draft: EntryDraft,
    occurred_at: datetime,
    embedding: list[float] | None,
    embedding_model: str | None,
    entities: tuple[ExtractedEntity, ...],
    entities_model: str | None,
) -> asyncpg.Record | None:
    # RETURNING, not a re-read: the supersession flip that follows in
    # ``create_entry`` never touches the new row, so this is its final state.
    return await conn.fetchrow(
        INSERT_ENTRY_RETURNING,
        entry_id,
        draft.kind.value,
        draft.summary,
        draft.body,
        json.dumps(draft.payload) if draft.payload is not None else None,  # jsonb
        json.dumps(encode_sources(draft.sources)),  # jsonb
        list(draft.tags),
        occurred_at,
        draft.author,
        draft.agent,
        draft.importance,
        draft.importance_source.value,
        draft.scope,
        draft.fleet_id,
        embedding,
        embedding_model,  # SPEC.md §7: model that produced the vector
        json.dumps(encode_entities(entities)),  # jsonb
        [e.name.lower() for e in entities],  # lower-cased names: the filter column
        entities_model,  # ADR 0016: extractor model (provenance)
    )


async def _flip_superseded(conn: asyncpg.Connection, entry_id: str, draft: EntryDraft) -> None:
    # ADR 0034: any target the guarded UPDATE did not flip
    # aborts the whole transaction, insert included.
    wanted = list(dict.fromkeys(draft.supersedes))
    flipped = {
        str(r["id"])
        for r in await conn.fetch(
            FLIP_SUPERSEDED,
            entry_id,
            valid_uuids(wanted),
            draft.fleet_id if draft.fleet_id and is_valid_uuid(draft.fleet_id) else None,
            draft.scope,
            draft.author,
        )
    }
    denied = [t for t in wanted if canonical_uuid(t) not in flipped]
    if denied:
        raise SupersedeConflict(denied)


class EntryStore(PgConnections):
    async def create_entry(
        self,
        draft: EntryDraft,
        embedding: list[float] | None = None,
        embedding_model: str | None = None,
        entities: tuple[ExtractedEntity, ...] = (),
        entities_model: str | None = None,
    ) -> Entry:
        """Insert a new entry and flip any ``draft.supersedes`` targets
        to ``superseded`` (SPEC.md §4.1), in one transaction so a crash
        cannot leave targets unflipped.
        """
        entry_id = str(uuid.uuid4())
        occurred_at = draft.resolved_occurred_at()

        async with self._write_transaction(draft.fleet_id) as conn:
            await refuse_merged(conn, draft.fleet_id)
            if draft.scope == SCOPE_FLEET:
                await refuse_stale_home(conn, draft.fleet_id, draft.author)
            row = await _insert_entry(
                conn,
                entry_id,
                draft,
                occurred_at,
                embedding,
                embedding_model,
                entities,
                entities_model,
            )
            if draft.see_also:
                await conn.execute(INSERT_LINKS, entry_id, valid_uuids(list(draft.see_also)))
            if draft.supersedes:
                await _flip_superseded(conn, entry_id, draft)
        if row is None:
            raise RuntimeError(f"insert of entry {entry_id} returned no row")
        entry = row_to_entry(row)
        if embedding is None:
            return entry
        # The writer gets its vector back; later reads do not fetch it.
        return replace(entry, embedding=tuple(float(x) for x in embedding))

    async def get_entry(self, entry_id: str) -> Entry | None:
        if not is_valid_uuid(entry_id):
            return None
        async with self._connection() as conn:
            row = await conn.fetchrow(SELECT_ENTRY, entry_id)
        return row_to_entry(row) if row is not None else None

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        ids = valid_uuids(entry_ids)
        if not ids:
            return {}
        async with self._connection() as conn:
            rows = await conn.fetch(SELECT_ENTRIES, ids)
        return {str(row["id"]): row_to_entry(row) for row in rows}

    async def list_predecessors(self, entry_ids: list[str]) -> list[Entry]:
        ids = valid_uuids(entry_ids)
        if not ids:
            return []
        async with self._connection() as conn:
            rows = await conn.fetch(SELECT_PREDECESSORS, ids)
        return [row_to_entry(row) for row in rows]

    async def entry_links(
        self, entry_id: str, limit: int, *, visibility: Visibility | None = None
    ) -> tuple[list[str], list[str]]:
        if not is_valid_uuid(entry_id):
            return [], []
        vis, vis_params = readable_clause(visibility, 2)
        sql = SELECT_LINKS_IN.format(vis=f" AND {vis}" if vis else "")
        async with self._connection() as conn:
            outgoing = await conn.fetch(SELECT_LINKS_OUT, entry_id)
            incoming = await conn.fetch(sql, entry_id, limit, *vis_params)
        return [str(r["to_id"]) for r in outgoing], [str(r["from_id"]) for r in incoming]

    async def entry_links_many(
        self, entry_ids: list[str], limit: int, *, visibility: Visibility | None = None
    ) -> dict[str, tuple[list[str], list[str]]]:
        canonical = {eid: canonical_uuid(eid) for eid in entry_ids}
        ids = list(dict.fromkeys(c for c in canonical.values() if c is not None))
        links: dict[str, tuple[list[str], list[str]]] = {c: ([], []) for c in ids}
        if ids:
            vis, vis_params = readable_clause(visibility, 2)
            sql = SELECT_LINKS_IN_MANY.format(vis=f" AND {vis}" if vis else "")
            async with self._connection() as conn:
                outgoing = await conn.fetch(SELECT_LINKS_OUT_MANY, ids)
                incoming = await conn.fetch(sql, ids, limit, *vis_params)
            for r in outgoing:
                links[str(r["from_id"])][0].append(str(r["to_id"]))
            for r in incoming:
                links[str(r["to_id"])][1].append(str(r["from_id"]))
        return {eid: links[c] if c is not None else ([], []) for eid, c in canonical.items()}

    async def upstream_links(
        self, entry_ids: list[str], *, visibility: Visibility | None = None
    ) -> dict[str, list[UpstreamLink]]:
        ids = valid_uuids(entry_ids)
        if not ids:
            return {}
        params: list[Any] = [ids]
        vis = visibility_clause(visibility, params)
        sql = SELECT_UPSTREAM_LINKS.format(vis=f" AND {vis}" if vis else "")
        async with self._connection() as conn:
            rows = await conn.fetch(sql, *params)
        out: dict[str, list[UpstreamLink]] = {}
        for r in rows:
            out.setdefault(str(r["from_id"]), []).append(
                UpstreamLink(
                    from_id=str(r["from_id"]),
                    to_id=str(r["to_id"]),
                    linked_at=r["linked_at"],
                    state=EntryState(r["state"]),
                    superseded_at=r["superseded_at"],
                    superseded_author=r["superseded_author"],
                    newest_flag_at=r["newest_flag_at"],
                    newest_noted_helpful_at=r["newest_noted_helpful_at"],
                )
            )
        return out

    async def list_entries(
        self,
        filters: EntryFilters,
        limit: int = 20,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[Entry]:
        """Filter-only listing (SPEC.md §5.1 ``GET /v1/entries``).

        When ``visibility`` is supplied, only entries visible to that
        reader are returned (ADR 0011); ``None`` keeps the v1 flat pool.
        """
        clauses, params = filter_conditions(filters, visibility)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = (
            "SELECT "
            + _ENTRY_COLUMNS
            + " FROM entries WHERE "
            + where
            + " ORDER BY created_at DESC, id DESC"
            + " LIMIT $"
            + str(len(params) + 1)
            + " OFFSET $"
            + str(len(params) + 2)
        )
        async with self._connection() as conn:
            rows = await conn.fetch(sql, *params, limit, offset)
        return [row_to_entry(row) for row in rows]

    async def count_entries(
        self, filters: EntryFilters, *, visibility: Visibility | None = None
    ) -> int:
        """Count entries matching ``filters`` with a SQL ``COUNT``;
        ``visibility`` as in ``list_entries``."""
        clauses, params = filter_conditions(filters, visibility)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = "SELECT count(*) AS n FROM entries WHERE " + where
        async with self._connection() as conn:
            row = await conn.fetchrow(sql, *params)
        return int(row["n"]) if row is not None else 0

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        """Flip an entry to ``withdrawn`` (SPEC.md §4.1).

        ``KeyError`` if unknown, ``ValueError`` if not active. Only the
        reason is persisted (there is no ``withdrawn_by`` column).
        """
        if not is_valid_uuid(entry_id):
            raise KeyError(f"unknown entry: {entry_id}")
        async with self._connection() as conn:
            existing = await conn.fetchrow(LOOKUP_STATE, entry_id)
            if existing is None:
                raise KeyError(f"unknown entry: {entry_id}")
            if existing["state"] != "active":
                raise ValueError(f"entry {entry_id} is {existing['state']}, not active")
            row = await conn.fetchrow(WITHDRAW, entry_id, reason)
            if row is None:
                # A concurrent flip won the guarded UPDATE: re-read to say why.
                state = await conn.fetchrow(LOOKUP_STATE, entry_id)
                if state is None:
                    raise KeyError(f"unknown entry: {entry_id}")
                raise ValueError(f"entry {entry_id} is {state['state']}, not active")
        return row_to_entry(row)

    async def redact_entry(self, entry_id: str) -> RedactOutcome:
        """Wipe the text of a withdrawn memory for good (ADR 0125): one
        transaction, every free-text field, the embedding and the keyword
        column. ``KeyError`` if unknown; ``RedactRefused`` if active or
        already redacted."""
        if not is_valid_uuid(entry_id):
            raise KeyError(f"unknown entry: {entry_id}")
        async with self._transaction() as conn:
            existing = await conn.fetchrow(REDACT_LOCK, entry_id)
            if existing is None:
                raise KeyError(f"unknown entry: {entry_id}")
            if existing["redacted_at"] is not None:
                raise RedactRefused(f"entry {entry_id} is already redacted")
            if existing["state"] == "active":
                raise RedactRefused(
                    f"entry {entry_id} is active: withdraw it first "
                    "(a superseded or withdrawn memory can be redacted)"
                )
            row = await conn.fetchrow(REDACT, entry_id, REDACTED_SUMMARY)
            if row is None:  # unreachable under the row lock; never report success on a miss
                raise RedactRefused(f"entry {entry_id} could not be redacted")
            await conn.execute(REDACT_CLEAR_TSV, entry_id)
            await conn.execute(REDACT_DROP_LINKS, entry_id)
            counts = await conn.fetchrow(REDACT_COUNT_NOTES, entry_id)
            reasons = await conn.fetchval(REDACT_COUNT_WITHDRAW_REASONS, entry_id)
        return RedactOutcome(
            row_to_entry(row),
            int(counts["notes"]) if counts else 0,
            int(counts["flags"]) if counts else 0,
            int(reasons or 0),
        )

    async def restore_entry(self, entry_id: str) -> Restoration:
        """Put a withdrawn entry back (ADR 0124), in one transaction that
        locks the row, checks the refusals and updates it."""
        if not is_valid_uuid(entry_id):
            raise KeyError(f"unknown entry: {entry_id}")
        async with self._transaction() as conn:
            await conn.execute(RESTORE_LOCK)
            author = await conn.fetchval(RESTORE_AUTHOR, entry_id)
            if author is None:
                raise KeyError(f"unknown entry: {entry_id}")
            await conn.execute(CLEANUP_AGENT_LOCK, author)
            existing = await conn.fetchrow(RESTORE_LOCK_ROW, entry_id)
            if existing is None:
                raise KeyError(f"unknown entry: {entry_id}")
            reason = existing["withdrawn_reason"]
            standing = None
            chain = None
            redacted = existing["redacted_at"] is not None or is_redaction_stub(
                existing["summary"], existing["body"], reason, existing["has_embedding"]
            )
            if existing["state"] == "withdrawn" and not redacted:
                found = await conn.fetchval(RESTORE_STANDING_CLEANUP, entry_id, reason)
                standing = str(found) if found is not None else None
                cut = await conn.fetchval(RESTORE_WAS_CUT, entry_id) is not None
                linked = await conn.fetchval(RESTORE_LINKED_BY, entry_id)
                instead = await conn.fetchval(RESTORE_PUT_BACK_INSTEAD, entry_id)
                chain = chain_conflict_message(
                    entry_id,
                    cut,
                    str(linked) if linked is not None else None,
                    str(instead) if instead is not None else None,
                )
            refusal = restore_refusal(
                entry_id, existing["state"], reason, standing, chain, redacted
            )
            if refusal is not None:
                raise refusal
            row = await conn.fetchrow(RESTORE, entry_id)
            if row is None:  # unreachable under the row lock
                raise RuntimeError(f"restore of entry {entry_id} updated no row")
        return Restoration(row_to_entry(row), reason)
