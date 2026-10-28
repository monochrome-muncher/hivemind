"""The Postgres adapter for the ``Store`` port (asyncpg + pgvector).

Mirrors ``hivemind.memstore.MemoryStore`` behavior-for-behavior
(SPEC.md §4, §6). The pool opens lazily on first use, so synchronous
builders can hand the adapter to a running loop (ADR 0007). SQL lives in
named constants; row mappers are pure functions at the bottom.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any

import asyncpg

from hivemind.domain.access import (
    Agent,
    AgentStatus,
    Fleet,
    FleetBusy,
    FleetExists,
    FleetMerged,
    InvalidAgentStatus,
    TrustLevel,
    UnknownAgent,
    UnknownFleet,
    Visibility,
)
from hivemind.domain.activity import (
    Activity,
    AuthorActivity,
    DayActivity,
    ReceivedFeedback,
    ReporterActivity,
)
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
from hivemind.domain.audit import (
    ActorKind,
    AuditAction,
    AuditEvent,
    AuditFilters,
    AuditRecord,
)
from hivemind.domain.entry import (
    EntityKind,
    Entry,
    EntryDraft,
    EntryFilters,
    EntryState,
    ExtractedEntity,
    ImportanceSource,
    Kind,
    SearchCount,
    Source,
    SourceType,
    UsageCount,
)
from hivemind.domain.feedback import (
    Correction,
    Feedback,
    FeedbackLogFilters,
    FeedbackLogItem,
    FeedbackSignals,
    FlagBacklogFilters,
    FlagCount,
    FlaggedEntry,
    Verdict,
    last_confirmed,
    truncate_note,
)
from hivemind.domain.fleet_merge import (
    KIND_MERGE,
    KIND_MOVE,
    DroppedPin,
    DuplicatePair,
    DuplicateScan,
    FleetFacts,
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
from hivemind.domain.health import (
    DISPERSION_CAP,
    DISPERSION_MIN,
    DISPERSION_TAGS,
    ECHO_SAMPLE,
    ECHO_THRESHOLD,
    ENTRY_ID_REF,
    Convergence,
    FleetHealth,
    HealthCounts,
    TagDispersion,
)
from hivemind.domain.pin import Pin, PinRefusal, PinWrite, pin_refusal
from hivemind.domain.succession import (
    AgentRetired,
    HandedDown,
    Succession,
    SuccessionRefused,
    UnknownSuccession,
)
from hivemind.domain.upstream import UpstreamLink
from hivemind.domain.validation import InvalidInput, canonical_uuid, is_valid_uuid
from hivemind.ports import SupersedeConflict
from hivemind.store.inheritance import INHERITED_FROM
from hivemind.store.pool import PoolTimeouts, make_pool

logger = logging.getLogger(__name__)

# --- SQL ----------------------------------------------------------------------

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

# Every ``Entry`` column except ``embedding``: no reader needs the stored
# vector, and fetching it was over half a 100-row list page's time.
# Entries read back carry ``embedding=None``.
_ENTRY_COLUMNS = """
    id, kind, summary, body, payload, sources, tags, occurred_at,
    created_at, author, agent, importance, importance_source, scope, fleet_id,
    embedding_model, state, superseded_by, withdrawn_reason,
    entities, entity_names, entities_model
"""

INSERT_ENTRY_RETURNING = INSERT_ENTRY.rstrip() + " RETURNING " + _ENTRY_COLUMNS

SELECT_ENTRY = "SELECT " + _ENTRY_COLUMNS + " FROM entries WHERE id = $1"

SELECT_ENTRIES = "SELECT " + _ENTRY_COLUMNS + " FROM entries WHERE id = ANY($1)"

# ADR 0074: every pair among the candidates, from the stored vectors (one
# self-join, no embedder call). Rows without a vector drop out of the join.
PAIRWISE_SIMILARITY = (
    "SELECT a.id AS a, b.id AS b, 1 - (a.embedding <=> b.embedding) AS similarity "
    "FROM entries a JOIN entries b ON a.id < b.id "
    "WHERE a.id = ANY($1) AND b.id = ANY($1) "
    "AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL"
)

# Reverse supersession link (PERF-1/STORE-2): served by the partial
# ``entries_superseded_by_idx`` (migration 0008); the predicate below must
# stay implied by the index's ``WHERE superseded_by IS NOT NULL``.
SELECT_PREDECESSORS = (
    "SELECT " + _ENTRY_COLUMNS + " FROM entries "
    "WHERE superseded_by = ANY($1) AND superseded_by IS NOT NULL"
)

# The grouped usage counters (PERF-2): one scan instead of 10 + 3*agents +
# fleets COUNTs.
USAGE_COUNTS = """
SELECT scope, kind, importance_source, author, fleet_id,
       (state = 'active') AS active, count(*) AS n
  FROM entries
 GROUP BY scope, kind, importance_source, author, fleet_id, (state = 'active')
"""

# One row per home fleet ('' = none), bumped once per first-page search
# (ADR 0056). Counts only: no query text, no agent name.
RECORD_SEARCH = """
INSERT INTO search_counts (fleet_id, searches, empty, explore) VALUES ($1, 1, $2, $3)
ON CONFLICT (fleet_id) DO UPDATE
   SET searches = search_counts.searches + 1,
       empty = search_counts.empty + EXCLUDED.empty,
       explore = search_counts.explore + EXCLUDED.explore
"""
SEARCH_COUNTS = "SELECT fleet_id, searches, empty, explore FROM search_counts"

# "See also" links (ADR 0057). The insert skips ids that name no entry
# (the join), and the reads are oldest link first.
INSERT_LINKS = """
INSERT INTO entry_links (from_id, to_id)
SELECT $1, e.id FROM entries e WHERE e.id = ANY($2::uuid[]) AND e.id <> $1
ON CONFLICT DO NOTHING
"""
SELECT_LINKS_OUT = "SELECT to_id FROM entry_links WHERE from_id = $1 ORDER BY created_at, to_id"
SELECT_LINKS_IN = """
SELECT from_id FROM entry_links WHERE to_id = $1
 ORDER BY created_at DESC, from_id LIMIT $2
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
       SELECT from_id, created_at FROM entry_links WHERE to_id = t.id
        ORDER BY created_at DESC, from_id LIMIT $2) AS l
 ORDER BY t.id, l.created_at DESC, l.from_id
"""

# Pinned entries (ADR 0058). The per-fleet advisory lock serialises
# pinners of one fleet, so the count check and the insert are atomic.
LOCK_FLEET_PINS = "SELECT pg_advisory_xact_lock(hashtextextended('hivemind.pins:' || $1, 0))"
INSERT_PIN = """
INSERT INTO pins (fleet_id, entry_id, pinned_by) VALUES ($1, $2, $3)
ON CONFLICT (fleet_id, entry_id) DO UPDATE SET pinned_by = EXCLUDED.pinned_by, pinned_at = now()
RETURNING fleet_id, entry_id, pinned_by, pinned_at, (xmax = 0) AS created
"""
DELETE_PIN = "DELETE FROM pins WHERE fleet_id = $1 AND entry_id = $2"
LIST_PINS = """
SELECT fleet_id, entry_id, pinned_by, pinned_at FROM pins WHERE fleet_id = $1
 ORDER BY pinned_at DESC, entry_id
"""

# --- Fleet merge SQL (ADR 0107) ---------------------------------------------

LOCK_FLEETS = """
SELECT f.id, f.name, f.created_at, f.merged_into, f.merged_at, t.name AS target_name
  FROM fleets f LEFT JOIN fleets t ON t.id = f.merged_into
 WHERE f.id = ANY($1::uuid[]) ORDER BY f.id FOR UPDATE OF f
"""
PINS_OF_FLEETS = """
SELECT fleet_id, entry_id, pinned_by, pinned_at FROM pins WHERE fleet_id = ANY($1::text[])
 ORDER BY pinned_at DESC, entry_id
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
PINNED_ENTRY_IDS = "SELECT entry_id, pinned_by FROM pins WHERE fleet_id = $1"
MOVE_RETURNING_PINS = """
SELECT fleet_id, entry_id, pinned_by, pinned_at FROM pins
 WHERE fleet_id = $1 AND entry_id = ANY($2::uuid[])
"""
MOVE_PIN_HOME = "UPDATE pins SET fleet_id = $2 WHERE fleet_id = $1 AND entry_id = $3"
RESTORE_PIN = """
INSERT INTO pins (fleet_id, entry_id, pinned_by, pinned_at) VALUES ($1, $2, $3, $4)
ON CONFLICT (fleet_id, entry_id) DO NOTHING
"""
CLEAR_MERGED = "UPDATE fleets SET merged_into = NULL, merged_at = NULL WHERE id = $1"
MARK_UNDONE = (
    "UPDATE fleet_merges SET undone_at = clock_timestamp() WHERE id = $1 RETURNING undone_at"
)
# --- Fleet move SQL (ADR 0112) ----------------------------------------------

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
MOVE_PIN_CANDIDATES = """
SELECT fleet_id, entry_id, pinned_by, pinned_at FROM pins
 WHERE (fleet_id = $1 AND entry_id = ANY($3::uuid[])) OR fleet_id = $2
 ORDER BY pinned_at DESC, entry_id
"""
REKEY_THESE_PINS = (
    "UPDATE pins SET fleet_id = $2 WHERE fleet_id = $1 AND entry_id = ANY($3::uuid[])"
)
AGENT_HOME = "SELECT home_fleet_id FROM agents WHERE name = $1"
FLEET_AGENT_COUNT = "SELECT count(*) FROM agents WHERE home_fleet_id = $1"
FLEET_ENTRY_STATES = "SELECT state, count(*) AS n FROM entries WHERE fleet_id = $1 GROUP BY state"
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

# --- Agent cleanup SQL (ADR 0111) --------------------------------------------

# The memories a cleanup withdraws: active, authored by $1, written in the
# window [$2, $3) (``created_at``: write time, not the memory date).
_CLEANUP_WITHDRAW_WHERE = (
    "author = $1 AND state = 'active' AND created_at >= $2 AND created_at < $3"
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
_CLEANUP_OWN = "SELECT id, state, superseded_by FROM entries WHERE author = $1 AND created_at >= $2 AND created_at < $3"
# Memories the agent's memories replaced (ADR 0111): superseded, not the
# agent's own window memories, and the ``superseded_by`` chain from them passes
# only through the agent's own window memories and ends at one that is still
# active (a memory the cleanup withdraws). ``first`` is the memory that
# replaced it, ``head`` the end of the chain. Nobody else's memory is in the
# chain, so a replacement made by another agent since is never undone.
_CLEANUP_RESTORABLE_CTES = f"""
own AS ({_CLEANUP_OWN}),
chain(root, first, cur) AS (
    SELECT e.id, e.superseded_by, e.superseded_by FROM entries e
     WHERE e.state = 'superseded' AND e.superseded_by IN (SELECT id FROM own)
       AND e.id NOT IN (SELECT id FROM own)
    UNION
    SELECT c.root, c.first, o.superseded_by FROM chain c JOIN own o ON o.id = c.cur
     WHERE o.state = 'superseded' AND o.superseded_by IN (SELECT id FROM own)
),
restorable AS (
    SELECT c.root AS id, c.first, c.cur AS head
      FROM chain c JOIN own h ON h.id = c.cur AND h.state = 'active'
)
"""
CLEANUP_RESTORABLE_LIST = (
    "WITH RECURSIVE "
    + _CLEANUP_RESTORABLE_CTES
    + "SELECT "
    + _ENTRY_COLUMNS
    + " FROM entries WHERE id IN (SELECT id FROM restorable)"
    " ORDER BY created_at DESC, id DESC LIMIT $4"
)
CLEANUP_RESTORABLE_COUNT = (
    "WITH RECURSIVE " + _CLEANUP_RESTORABLE_CTES + "SELECT count(*) FROM restorable"
)
CLEANUP_AGENT_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended('hivemind.cleanup:' || $1, 0))"
CLEANUP_WITHDRAW = """
UPDATE entries SET state = 'withdrawn', withdrawn_reason = $2, withdrawn_at = now()
 WHERE id = ANY($1::uuid[]) AND state = 'active'
"""
CLEANUP_RESTORABLE_LOCK = (
    "WITH RECURSIVE "
    + _CLEANUP_RESTORABLE_CTES
    + "SELECT e.id, r.first, r.head FROM entries e JOIN restorable r ON r.id = e.id"
    " ORDER BY e.id FOR UPDATE OF e"
)
CLEANUP_RESTORE = """
UPDATE entries SET state = 'active', superseded_by = NULL
 WHERE id = ANY($1::uuid[]) AND state = 'superseded'
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


CLEANUP_PINS_COUNT = (
    "WITH RECURSIVE "
    + _CLEANUP_RESTORABLE_CTES
    + ", "
    + _pin_walk("w.cur NOT IN (SELECT id FROM restorable)")
    + "SELECT count(DISTINCT pinned) FROM walk"
    " WHERE cur IN (SELECT id FROM own WHERE state = 'active')"
    " AND pinned NOT IN (SELECT id FROM restorable)"
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
                            restored, ratings_set_aside, unpinned_pins)
VALUES ($1, $2, $3, $4, $5, clock_timestamp(), $6, $7, $8, $9)
RETURNING cleaned_at
"""
_CLEANUP_COLUMNS = (
    "id, agent, since, until, cleaned_by, cleaned_at, withdrawn_ids, restored, "
    "ratings_set_aside, unpinned_pins, revoked, undone_at"
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
CLEANUP_LOCK_ENTRIES = "SELECT id FROM entries WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE"
CLEANUP_REACTIVATE = """
UPDATE entries SET state = 'active', withdrawn_reason = NULL, withdrawn_at = NULL
 WHERE id = ANY($1::uuid[]) AND state = 'withdrawn' AND withdrawn_reason = $2
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

# --- Fleet / agent access-control SQL (ADRs 0011-0012) ---------------------

SELECT_FLEET_BY_NAME = "SELECT 1 FROM fleets WHERE name = $1"
INSERT_FLEET = (
    "INSERT INTO fleets (name) VALUES ($1) "
    "ON CONFLICT (name) DO NOTHING RETURNING id, name, created_at, merged_into, merged_at"
)
RENAME_FLEET = (
    "UPDATE fleets SET name = $2 WHERE id = $1 "
    "RETURNING id, name, created_at, merged_into, merged_at"
)
GET_FLEET_FOR_UPDATE = (
    "SELECT id, name, created_at, merged_into, merged_at FROM fleets WHERE id = $1 FOR UPDATE"
)
GET_FLEET = "SELECT id, name, created_at, merged_into, merged_at FROM fleets WHERE id = $1"
LIST_FLEETS = "SELECT id, name, created_at, merged_into, merged_at FROM fleets ORDER BY created_at"
# ADR 0107: a write into a fleet holds a share lock on its row for the rest of
# its transaction, so a merge (FOR UPDATE) waits for it and, once committed,
# the next write sees `merged_into` and is refused.
# Merge and undo: wait up to 5 s for in-flight writes, and run without the
# server's statement timeout (the client side is `LONG_OP_TIMEOUT`).
MERGE_SETTINGS = """
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = 0;
"""
WRITE_LOCK_TIMEOUT = "3s"  # how long a fleet write waits for a merge before a 503
# A merge or undo moves every row of a fleet in one transaction: it can run
# longer than the pool's command timeout (30 s) and any statement timeout.
LONG_OP_TIMEOUT = 3600.0
GUARD_FLEET = """
SELECT f.merged_into, t.name AS target_name
  FROM fleets f LEFT JOIN fleets t ON t.id = f.merged_into
 WHERE f.id = $1 FOR SHARE OF f
"""

GET_AGENT = "SELECT * FROM agents WHERE name = $1"
# Agent names are ASCII (ADR 0040), so lower() is their case fold. The oldest
# holder wins when legacy case variants already coexist (ADR 0045).
GET_AGENT_FOLDED = "SELECT * FROM agents WHERE lower(name) = lower($1) ORDER BY created_at LIMIT 1"
# Serialises registrations of one case-folded name, so two concurrent
# registrations of `Bob` and `bob` cannot both pass the folded check.
AGENT_NAME_LOCK = "SELECT pg_advisory_xact_lock(hashtext('hivemind:agent-name:' || lower($1)))"
INSERT_AGENT = (
    "INSERT INTO agents (name, owner_alias, status, trust_level) "
    "VALUES ($1, $2, 'pending', 0) ON CONFLICT (name) DO NOTHING RETURNING *"
)
# The agent's key use (ADR 0110) rides along; an agent has at most one key
# (unique index, ADR 0039), so the join adds no rows.
LIST_AGENTS = (
    "SELECT a.*, c.last_used_at, c.created_at AS key_created_at, "
    "c.locked_at AS key_locked_at FROM agents a "
    "LEFT JOIN credentials c ON c.kind = 'agent' AND c.agent_name = a.name "
    "ORDER BY a.name"
)
# ADR 0028: the status guard lives in the WHERE, so check-and-flip is one
# atomic statement (no second key from two racing activations).
ACTIVATE_AGENT = (
    "UPDATE agents SET status = 'active', trust_level = $2, home_fleet_id = $3, "
    "activated_at = now() WHERE name = $1 AND status IN ('pending', 'revoked') RETURNING *"
)
REVOKE_AGENT = (
    "UPDATE agents SET status = 'revoked' "
    "WHERE name = $1 AND status IN ('pending', 'active') RETURNING *"
)
# --- Successions SQL (ADR 0113) -------------------------------------------------

LOCK_AGENT_ROWS = (
    "SELECT name, status FROM agents WHERE name = ANY($1::text[]) ORDER BY name FOR UPDATE"
)
_SUCCESSION_COLUMNS = 'retired_agent, successor, "by", at'
GET_SUCCESSION = f"SELECT {_SUCCESSION_COLUMNS} FROM successions WHERE retired_agent = $1"
LIST_SUCCESSIONS = f"SELECT {_SUCCESSION_COLUMNS} FROM successions ORDER BY at DESC, retired_agent"
INSERT_SUCCESSION = (
    'INSERT INTO successions (retired_agent, successor, "by") VALUES ($1, $2, $3) '
    f"RETURNING {_SUCCESSION_COLUMNS}"
)
DELETE_SUCCESSION = (
    f"DELETE FROM successions WHERE retired_agent = $1 RETURNING {_SUCCESSION_COLUMNS}"
)
REVOKE_ACTIVE_AGENT = "UPDATE agents SET status = 'revoked' WHERE name = $1 AND status = 'active'"
HANDED_DOWN = """
SELECT count(*) FILTER (WHERE e.scope = 'self') AS self_notes,
       count(*) FILTER (WHERE e.scope = 'fleet') AS fleet_memories,
       count(*) FILTER (WHERE e.scope = 'fleet'
                          AND e.fleet_id IS DISTINCT FROM $2::uuid) AS fleet_elsewhere
  FROM entries e WHERE e.state = 'active' AND e.author = ANY($1::text[])
"""
HANDED_DOWN_PINS = "SELECT count(*) FROM pins WHERE pinned_by = ANY($1::text[])"

SET_AGENT_LEVEL = "UPDATE agents SET trust_level = $2 WHERE name = $1 RETURNING *"
SET_AGENT_FLEET = "UPDATE agents SET home_fleet_id = $2 WHERE name = $1 RETURNING *"

FLIP_SUPERSEDED = """
UPDATE entries
SET state = 'superseded', superseded_by = $1
WHERE id = ANY($2) AND state = 'active'
RETURNING id
"""

LOOKUP_STATE = "SELECT state FROM entries WHERE id = $1"

WITHDRAW = (
    "UPDATE entries SET state = 'withdrawn', withdrawn_reason = $2, withdrawn_at = now() "
    "WHERE id = $1 AND state = 'active' RETURNING " + _ENTRY_COLUMNS
)

# --- Keyword-stream query (ADR 0047) ------------------------------------------

MAX_KEYWORD_TERMS = 16
"""The most distinct query terms the keyword stream matches on.

Caps ``ts_rank`` cost: a long any-term query of common words would
otherwise cost seconds of CPU. The vector stream still sees the whole query."""

ANY_TERM_TSQUERY = (
    "(SELECT coalesce(string_agg(term, ' | ' ORDER BY first), '')::tsquery"
    " FROM (SELECT term, min(pos) AS first"
    " FROM unnest(string_to_array(plainto_tsquery('english', {param})::text, ' & '))"
    " WITH ORDINALITY AS t(term, pos)"
    f" GROUP BY term ORDER BY first LIMIT {MAX_KEYWORD_TERMS}) AS terms)"
)
"""The keyword stream's tsquery: any query term matches (ADR 0047), on
at most ``MAX_KEYWORD_TERMS`` distinct terms (ADR 0049).

``plainto_tsquery`` does the parsing, so caller text is never read as
tsquery syntax (no operator injection). Its ``' & '``-joined lexemes
(never containing a space) are split, deduplicated in query order, and
re-joined with ``' | '``. A stop-words-only query matches nothing. The
scalar subquery runs once per statement.
"""

# --- Vector-search session settings (ADR 0025) --------------------------------

VECTOR_SEARCH_SETTINGS = """
SET LOCAL hnsw.iterative_scan = strict_order;
SET LOCAL hnsw.ef_search = 40;
"""
"""The query-time GUCs the HNSW index (migration ``0004``) is read under.

``SET LOCAL`` in the search's transaction, not a pool ``init`` callback:
behind a transaction-mode PgBouncer (``store/pool.py``) a session ``SET``
lands on an arbitrary backend and is discarded before the search runs.
``SET LOCAL`` travels with the transaction and unsets at commit.

``iterative_scan`` (pgvector >= 0.8) keeps scanning until the filtered
``LIMIT`` is met; without it HNSW filters ``ef_search`` candidates
afterwards and a narrow-visibility reader can starve RRF's vector list.
``strict_order`` because RRF fuses on ranks (``retrieval/rrf.py``), which
``relaxed_order`` would perturb. ``ef_search = 40`` is pgvector's default,
stated explicitly; untuned for now (ADR 0025).
"""

# ADR 0067: a revoked agent's reports are left out of every feedback read
# (the rows stay, so re-activation brings them back). ``agents.name`` is
# unique, so this is an index probe; a reporter with no agent record counts.
# ADR 0111: so are the rows an admin cleanup set aside (``set_aside_by``).
# Every read of ``feedbacks`` as ``f`` uses this one condition.
_RATING_COUNTS = (
    "f.set_aside_by IS NULL AND "
    "NOT EXISTS (SELECT 1 FROM agents a WHERE a.name = f.\"user\" AND a.status = 'revoked')"
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

# The admin activity view (ADR 0080): plain grouped scans, one per figure.
# ``$1`` is the window start. Revoked reporters are left out (ADR 0067).
ACTIVITY_AUTHORS = """
SELECT author, fleet_id, count(*) AS entries,
       count(*) FILTER (WHERE state = 'active') AS active,
       count(*) FILTER (WHERE created_at >= $1) AS recent,
       max(created_at) AS last_write_at
  FROM entries GROUP BY author, fleet_id
"""
ACTIVITY_REPORTERS = f"""
SELECT f."user" AS reporter, count(*) AS verdicts,
       count(*) FILTER (WHERE f.updated_at >= $1) AS recent,
       count(*) FILTER (WHERE f.verdict = 'helpful') AS helpful,
       count(*) FILTER (WHERE f.verdict = 'stale') AS stale,
       count(*) FILTER (WHERE f.verdict = 'wrong') AS wrong,
       max(f.updated_at) AS last_at
  FROM feedbacks f WHERE {_RATING_COUNTS}
 GROUP BY f."user"
"""
ACTIVITY_RECEIVED = f"""
SELECT e.author, count(*) FILTER (WHERE f.verdict = 'helpful') AS helpful,
       count(*) FILTER (WHERE f.verdict = 'stale') AS stale,
       count(*) FILTER (WHERE f.verdict = 'wrong') AS wrong
  FROM feedbacks f JOIN entries e ON e.id = f.entry_id
 WHERE {_RATING_COUNTS}
 GROUP BY e.author
"""
ACTIVITY_DAYS = f"""
SELECT day, sum(writes)::int AS writes, sum(helpful)::int AS helpful,
       sum(stale)::int AS stale, sum(wrong)::int AS wrong
  FROM (
    SELECT (created_at AT TIME ZONE 'UTC')::date AS day, 1 AS writes,
           0 AS helpful, 0 AS stale, 0 AS wrong
      FROM entries WHERE created_at >= $1
    UNION ALL
    SELECT (f.updated_at AT TIME ZONE 'UTC')::date, 0,
           (f.verdict = 'helpful')::int, (f.verdict = 'stale')::int, (f.verdict = 'wrong')::int
      FROM feedbacks f WHERE f.updated_at >= $1 AND {_RATING_COUNTS}
  ) t
 GROUP BY day ORDER BY day
"""

# ``updated_at`` defaults to the database clock, like ``pinned_at`` and
# ``created_at``, so feedback compares truthfully with them (ADR 0079).
UPSERT_FEEDBACK = """
INSERT INTO feedbacks (entry_id, "user", agent, verdict, note, updated_at)
VALUES ($1, $2, $3, $4, $5, COALESCE($6, now()))
ON CONFLICT (entry_id, "user", agent) DO UPDATE
SET verdict = EXCLUDED.verdict,
    note = EXCLUDED.note,
    updated_at = EXCLUDED.updated_at,
    set_aside_by = NULL
RETURNING updated_at
"""

# The verdict counts, spelled once for every counting statement below.
_VERDICT_COUNTS = """COUNT(*) FILTER (WHERE verdict = 'helpful') AS helpful,
    COUNT(*) FILTER (WHERE verdict = 'stale') AS stale,
    COUNT(*) FILTER (WHERE verdict = 'wrong') AS wrong"""

COUNT_FEEDBACK = f"""
SELECT
    {_VERDICT_COUNTS}
FROM feedbacks f
WHERE entry_id = $1 AND {_RATING_COUNTS}
"""

LIST_FEEDBACK = f"""
SELECT entry_id, "user", agent, verdict, note, updated_at
FROM feedbacks f
WHERE entry_id = $1 AND {_RATING_COUNTS}
ORDER BY (verdict = 'helpful' AND COALESCE(note, '') = ''), updated_at DESC, "user", agent
LIMIT $2
"""

# The first $2 rows of each entry in $1 (ADR 0059), in LIST_FEEDBACK order
# (informative rows before bare helpful ones, ADR 0064).
LIST_FEEDBACK_MANY = f"""
SELECT entry_id, "user", agent, verdict, note, updated_at FROM (
    SELECT f.*, row_number() OVER (
               PARTITION BY entry_id
               ORDER BY (verdict = 'helpful' AND COALESCE(note, '') = ''), updated_at DESC, "user", agent) AS rn
      FROM feedbacks f
     WHERE entry_id = ANY($1::uuid[]) AND {_RATING_COUNTS}
) AS ranked
WHERE rn <= $2
ORDER BY entry_id, (verdict = 'helpful' AND COALESCE(note, '') = ''), updated_at DESC, "user", agent
"""

# The feedback log: rows across entries with the rated entry's list fields.
# ``{where}`` is built by ``list_feedback_log``.
FEEDBACK_LOG = """
SELECT f.entry_id, f."user", f.agent, f.verdict, f.note, f.updated_at,
       e.kind, e.summary, e.author, e.scope, e.fleet_id, e.state
  FROM feedbacks f JOIN entries e ON e.id = f.entry_id
 WHERE {where}
 ORDER BY f.updated_at DESC, f.entry_id, f."user", f.agent
 LIMIT ${limit} OFFSET ${offset}
"""

# The flag backlog (ADR 0093): active entries with a standing stale/wrong
# verdict, oldest first flag first. $1 is now; "orphaned" is
# HEALTH_BY_FLEET's rule (author revoked, or no write in the 720 hours
# before now). {inner} filters the entry (visibility, fleet), {outer} the
# listed row (first flag age, orphaned).
FLAG_BACKLOG = f"""
WITH flagged AS (
    SELECT f.entry_id,
           min(f.updated_at) FILTER (WHERE f.verdict IN ('stale', 'wrong')) AS first_flag,
           max(f.updated_at) FILTER (WHERE f.verdict IN ('stale', 'wrong')) AS last_flag,
           {_VERDICT_COUNTS}
      FROM feedbacks f
     WHERE {_RATING_COUNTS}
     GROUP BY f.entry_id
    HAVING count(*) FILTER (WHERE f.verdict IN ('stale', 'wrong')) > 0
), backlog AS (
    SELECT e.id, e.kind, e.summary, e.author, e.scope, e.fleet_id,
           fl.first_flag, fl.last_flag, fl.helpful, fl.stale, fl.wrong,
           coalesce(au.status = 'revoked', FALSE)
             OR coalesce(lw.at < $1::timestamptz - interval '720 hours', FALSE) AS orphaned
      FROM flagged fl
      JOIN entries e ON e.id = fl.entry_id
      LEFT JOIN agents au ON au.name = e.author
      LEFT JOIN LATERAL (
          SELECT max(w.created_at) AS at FROM entries w WHERE w.author = e.author
      ) lw ON TRUE
     WHERE e.state = 'active' AND {{inner}}
)
SELECT * FROM backlog
 WHERE {{outer}}
 ORDER BY first_flag, id
 LIMIT ${{limit}} OFFSET ${{offset}}
"""

BATCH_FEEDBACK = f"""
SELECT
    entry_id,
    {_VERDICT_COUNTS}
FROM feedbacks f
WHERE entry_id = ANY($1) AND {_RATING_COUNTS}
GROUP BY entry_id
"""

# BATCH_FEEDBACK's counts plus the two newest dates "last confirmed" is made
# of (SPEC §4.2): the newest helpful row with a note, the newest flag (also
# what a pin's flagged check reads, ADR 0076).
BATCH_FEEDBACK_SIGNALS = f"""
SELECT g.*, corr.verdict AS corr_verdict, corr.note AS corr_note,
       corr."user" AS corr_user, corr.updated_at AS corr_updated_at
FROM (
    SELECT
        entry_id,
        {_VERDICT_COUNTS},
        MAX(updated_at) FILTER (
            WHERE verdict = 'helpful' AND coalesce(note, '') ~ '\\S'
        ) AS newest_noted_helpful_at,
        MAX(updated_at) FILTER (WHERE verdict IN ('stale', 'wrong')) AS newest_flag_at
    FROM feedbacks f
    WHERE entry_id = ANY($1) AND {_RATING_COUNTS}
    GROUP BY entry_id
) g
LEFT JOIN LATERAL (
    SELECT f.verdict, f.note, f."user", f.updated_at
      FROM feedbacks f
     WHERE f.entry_id = g.entry_id
       AND f.verdict IN ('stale', 'wrong')
       AND coalesce(f.note, '') ~ '\\S'
       AND {_RATING_COUNTS}
     ORDER BY f.updated_at DESC, f."user", f.agent
     LIMIT 1
) corr ON true
"""

# Per entry in $1, how many entries it replaced (the reverse supersession
# link, served by ``entries_superseded_by_idx``), counting only those the
# reader may read: ``{vis}`` is the visibility clause, whose columns resolve
# to ``entries`` here (ADR 0011, ADR 0033).
COUNT_SUPERSEDES = """
SELECT superseded_by AS entry_id, COUNT(*) AS n
FROM entries
WHERE superseded_by = ANY($1) AND superseded_by IS NOT NULL{vis}
GROUP BY superseded_by
"""

# ADR 0068: per reporter, the stale/wrong rows written while the entry was
# still current, and the ones whose entry was superseded after the row.
# Entries carry no superseded_at, so the successor's created_at stands in.
# Both joins are primary-key probes.
FLAG_COUNTS = f"""
SELECT f."user" AS reporter,
       COUNT(*) AS flags,
       COUNT(*) FILTER (WHERE s.id IS NOT NULL) AS confirmed
FROM feedbacks f
JOIN entries e ON e.id = f.entry_id
LEFT JOIN entries s ON s.id = e.superseded_by AND e.state = 'superseded'
WHERE f.verdict IN ('stale', 'wrong')
  AND (s.id IS NULL OR s.created_at > f.updated_at)
  AND {_RATING_COUNTS}
GROUP BY f."user"
"""

# Fleet health metrics. $1 is "now"; the windows are read off it.
# Four statements run one after the other in one read-only REPEATABLE READ
# transaction, so they share a snapshot. Each is dominated by a pass over
# the wide ``entries`` heap; fusing them through a materialised CTE spills
# past work_mem and is slower than the passes it saves. Windows are
# hour-based so they do not depend on the server TimeZone.
#
# The per-fleet entry counters are two passes, summed per fleet: one over
# the active entries (sources, kind, tags, joined to the small sets of
# entries with recent feedback and insights linking >= 2 entries), and one
# from the standing stale/wrong verdicts to their active entries (the last
# write of each flagged entry's author is one index probe per author).
HEALTH_BY_FLEET = f"""
WITH flagged AS (
    SELECT e.id, e.fleet_id, e.author, min(f.updated_at) AS first_flag
      FROM feedbacks f JOIN entries e ON e.id = f.entry_id
     WHERE f.verdict IN ('stale', 'wrong') AND e.state = 'active' AND {_RATING_COUNTS}
     GROUP BY e.id
), last_write AS (
    SELECT fa.author,
           (SELECT max(w.created_at) FROM entries w WHERE w.author = fa.author) AS at
      FROM (SELECT DISTINCT author FROM flagged) fa
), fresh AS (
    SELECT DISTINCT f.entry_id FROM feedbacks f
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
       AND {_RATING_COUNTS}
), linking AS (
    SELECT from_id FROM entry_links GROUP BY from_id HAVING count(*) >= 2
), parts AS (
    SELECT e.fleet_id,
           count(*) FILTER (WHERE e.scope IN ('org', 'fleet')
                              AND e.kind IN ('fact', 'insight')) AS sourceable,
           count(*) FILTER (WHERE e.scope IN ('org', 'fleet')
                              AND e.kind IN ('fact', 'insight')
                              AND jsonb_array_length(e.sources) > 0) AS sourced,
           count(*) FILTER (WHERE e.kind = 'insight') AS insights,
           count(*) FILTER (WHERE e.kind = 'insight' AND linking.from_id IS NOT NULL) AS synthesizing,
           0::bigint AS backlog, 0::bigint AS orphaned,
           count(*) FILTER (WHERE e.occurred_at < $1::timestamptz - interval '2160 hours'
                              AND fresh.entry_id IS NULL) AS floating,
           count(*) FILTER (WHERE 'anomaly' = ANY(e.tags)) AS gap_anomaly,
           count(*) FILTER (WHERE 'open-question' = ANY(e.tags)) AS gap_open_question,
           count(*) FILTER (WHERE 'dead-end' = ANY(e.tags)) AS gap_dead_end
      FROM entries e
      LEFT JOIN fresh ON fresh.entry_id = e.id
      LEFT JOIN linking ON linking.from_id = e.id
     WHERE e.state = 'active'
     GROUP BY e.fleet_id
  UNION ALL
    SELECT fl.fleet_id, 0, 0, 0, 0,
           count(*) FILTER (WHERE fl.first_flag < $1::timestamptz - interval '336 hours'),
           count(*) FILTER (WHERE a.status = 'revoked'
                              OR lw.at < $1::timestamptz - interval '720 hours'),
           0, 0, 0, 0
      FROM flagged fl
      LEFT JOIN agents a ON a.name = fl.author
      LEFT JOIN last_write lw ON lw.author = fl.author
     GROUP BY fl.fleet_id
)
SELECT fleet_id, sum(sourceable) AS sourceable, sum(sourced) AS sourced,
       sum(insights) AS insights, sum(synthesizing) AS synthesizing,
       sum(backlog) AS backlog, sum(orphaned) AS orphaned, sum(floating) AS floating,
       sum(gap_anomaly) AS gap_anomaly, sum(gap_open_question) AS gap_open_question,
       sum(gap_dead_end) AS gap_dead_end
  FROM parts
 GROUP BY fleet_id
"""


# An owner alias as the sibling and owner metrics compare it (ADR 0089):
# trimmed of ASCII whitespace, lower-cased, blank = none. ``owner_key`` in
# ``domain/health.py`` is the same rule for ``MemoryStore``.
def _owner_key(column: str) -> str:
    return f"nullif(lower(btrim({column}, E' \\t\\r\\n\\f\\v')), '')"


# Fleet-wide scalars in one round trip: helpful verdicts, the 90-day writes
# and verdicts with their top-3 concentration, the 30-day writes and
# verdicts by agent label, grounding of the 30-day insights and decisions
# ($2 is the pattern of a source ref that only names an entry),
# supersessions (counted when the successor was written) and self -> fleet
# promotions; and the owner-level verdict figures (ADR 0089).
HEALTH_SCALARS = f"""
WITH writes AS (
    SELECT author, count(*) AS n FROM entries
     WHERE created_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
     GROUP BY author
), verdicts AS (
    SELECT f."user" AS reporter, count(*) AS n FROM feedbacks f
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
       AND {_RATING_COUNTS}
     GROUP BY f."user"
), owners AS (
    SELECT CASE WHEN o.key IS NOT NULL THEN 'o:' || o.key ELSE 'r:' || f."user" END AS owner,
           count(*) AS n
      FROM feedbacks f
      LEFT JOIN LATERAL (SELECT {_owner_key("a.owner_alias")} AS key
                           FROM agents a WHERE a.name = f."user") o ON true
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
       AND {_RATING_COUNTS}
     GROUP BY 1
), siblings AS (
    SELECT count(*) AS n
      FROM feedbacks f
      JOIN entries e ON e.id = f.entry_id
      JOIN agents ra ON ra.name = f."user"
      JOIN agents ea ON ea.name = e.author
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
       AND {_RATING_COUNTS}
       AND ra.name <> ea.name
       AND {_owner_key("ra.owner_alias")} = {_owner_key("ea.owner_alias")}
), helpful AS (
    SELECT count(*) AS n, count(*) FILTER (WHERE f.note ~ '\\S') AS noted
      FROM feedbacks f WHERE f.verdict = 'helpful' AND {_RATING_COUNTS}
), grounding AS (
    SELECT count(*) AS n,
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM jsonb_array_elements(e.sources) src
                WHERE src->>'ref' !~* $2)) AS grounded
      FROM entries e
     WHERE e.created_at BETWEEN $1::timestamptz - interval '720 hours' AND $1::timestamptz
       AND e.scope IN ('org', 'fleet') AND e.kind IN ('insight', 'decision')
), writes30 AS (
    SELECT agent, count(*) AS n FROM entries
     WHERE created_at BETWEEN $1::timestamptz - interval '720 hours' AND $1::timestamptz
       AND agent ~ '\\S'
     GROUP BY agent
), verdicts30 AS (
    SELECT f.agent, count(*) AS n FROM feedbacks f
     WHERE f.updated_at BETWEEN $1::timestamptz - interval '720 hours' AND $1::timestamptz
       AND f.agent ~ '\\S' AND {_RATING_COUNTS}
     GROUP BY f.agent
), chains AS (
    SELECT count(*) AS supersessions,
           count(DISTINCT n.id) FILTER (WHERE n.scope = 'fleet' AND s.scope = 'self') AS promotions
      FROM entries n JOIN entries s ON s.superseded_by = n.id
     WHERE n.created_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
)
SELECT (SELECT n FROM helpful) AS helpful,
       (SELECT noted FROM helpful) AS helpful_noted,
       (SELECT coalesce(sum(n), 0) FROM writes) AS writes,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM writes ORDER BY n DESC LIMIT 3) t) AS top_authors,
       (SELECT count(*) FROM writes) AS authors,
       (SELECT coalesce(sum(n), 0) FROM verdicts) AS verdicts,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM verdicts ORDER BY n DESC LIMIT 3) t) AS top_reporters,
       (SELECT count(*) FROM verdicts) AS reporters,
       (SELECT n FROM siblings) AS sibling_verdicts,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM owners ORDER BY n DESC LIMIT 3) t) AS top_owners,
       (SELECT count(*) FROM owners) AS owners,
       (SELECT supersessions FROM chains) AS supersessions,
       (SELECT promotions FROM chains) AS promotions,
       (SELECT n FROM grounding) AS synthesis_writes,
       (SELECT grounded FROM grounding) AS grounded,
       (SELECT coalesce(sum(n), 0) FROM writes30) AS writes30,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM writes30 ORDER BY n DESC LIMIT 3) t) AS top_write_labels,
       (SELECT count(*) FROM writes30) AS write_labels,
       (SELECT coalesce(sum(n), 0) FROM verdicts30) AS verdicts30,
       (SELECT coalesce(sum(n), 0) FROM (SELECT n FROM verdicts30 ORDER BY n DESC LIMIT 3) t) AS top_verdict_labels,
       (SELECT count(*) FROM verdicts30) AS verdict_labels
"""

# Ages in seconds of the active entries that show rivals alive:
# tagged ``alternative``, a summary starting "Conflict:", and contested ones
# (a live helpful verdict beside a live stale/wrong one).
HEALTH_RIVALS = f"""
SELECT 'alternative' AS kind, extract(epoch FROM ($1::timestamptz - e.created_at))::float8 AS age
  FROM entries e WHERE e.state = 'active' AND 'alternative' = ANY(e.tags)
UNION ALL
SELECT 'conflict', extract(epoch FROM ($1::timestamptz - e.created_at))::float8
  FROM entries e WHERE e.state = 'active' AND e.summary LIKE 'Conflict:%'
UNION ALL
SELECT 'contested', extract(epoch FROM ($1::timestamptz - e.created_at))::float8
  FROM entries e
 WHERE e.state = 'active'
   AND e.id IN (SELECT f.entry_id FROM feedbacks f WHERE {_RATING_COUNTS}
                 GROUP BY f.entry_id
                HAVING bool_or(f.verdict = 'helpful') AND bool_or(f.verdict <> 'helpful'))
"""

# Echo rate: for each of the newest $2 writes of the last 30 days, the
# nearest earlier active entry (one HNSW lookup each, the same settings as
# search); an echo when its similarity is >= $3.
CONVERGENCE_ECHO = """
WITH w AS (
    SELECT id, embedding, created_at FROM entries
     WHERE created_at >= $1::timestamptz - interval '720 hours' AND created_at <= $1::timestamptz
       AND embedding IS NOT NULL
     ORDER BY created_at DESC, id DESC LIMIT $2
)
SELECT count(*) AS sampled, count(*) FILTER (WHERE nn.sim >= $3::float8 AND nn.sim <> 'NaN'::float8) AS echoes
  FROM w
  LEFT JOIN LATERAL (
      SELECT 1 - (e.embedding <=> w.embedding) AS sim FROM entries e
       WHERE e.state = 'active' AND e.embedding IS NOT NULL
         AND (e.created_at, e.id) < (w.created_at, w.id)
       ORDER BY e.embedding <=> w.embedding LIMIT 1) nn ON true
"""

# Topic dispersion: the $3 busiest tags (distinct tags of org- and
# fleet-scope entries) with >= $4 entries in both 30-day windows, chosen
# from tags and counts alone (no vectors read); the $2 newest entries per
# tag and window; then the embeddings of just those ids, and the mean
# pairwise cosine inside each window (a zero-norm vector counts as 0). The
# vectors are normalised once as they are fetched (cosine is unchanged):
# the column is TOASTed, and detoasting it for every pair was most of the cost.
CONVERGENCE_DISPERSION = """
WITH tagged AS (
    SELECT DISTINCT t.tag, e.id, e.created_at,
           (e.created_at >= $1::timestamptz - interval '720 hours') AS recent
      FROM entries e, unnest(e.tags) AS t(tag)
     WHERE e.created_at BETWEEN $1::timestamptz - interval '1440 hours' AND $1::timestamptz
       AND e.embedding IS NOT NULL AND e.scope IN ('org', 'fleet')
), busiest AS (
    SELECT tag, count(*) AS n, count(*) FILTER (WHERE recent) AS n_recent,
           count(*) FILTER (WHERE NOT recent) AS n_before
      FROM tagged GROUP BY tag
    HAVING count(*) FILTER (WHERE recent) >= $4 AND count(*) FILTER (WHERE NOT recent) >= $4
     ORDER BY n DESC, tag COLLATE "C" LIMIT $3
), capped AS (
    SELECT tag, id, recent FROM (
        SELECT t.tag, t.id, t.recent,
               row_number() OVER (PARTITION BY t.tag, t.recent
                                  ORDER BY t.created_at DESC, t.id DESC) AS rn
          FROM tagged t JOIN busiest b ON b.tag = t.tag) ranked
     WHERE rn <= $2
), vectors AS MATERIALIZED (
    SELECT c.tag, c.id, c.recent,
           (SELECT l2_normalize(e.embedding) FROM entries e WHERE e.id = c.id) AS embedding
      FROM capped c
), pairs AS (
    SELECT a.tag, a.recent,
           avg(CASE WHEN d.dist = 'NaN'::float8 THEN 0 ELSE 1 - d.dist END) AS cosine
      FROM vectors a
      JOIN vectors b ON b.tag = a.tag AND b.recent = a.recent AND a.id < b.id
      CROSS JOIN LATERAL (SELECT a.embedding <=> b.embedding AS dist) d
     GROUP BY a.tag, a.recent
)
SELECT b.tag, least(b.n_recent, $2) AS n_recent, least(b.n_before, $2) AS n_before,
       max(p.cosine) FILTER (WHERE p.recent) AS recent_cosine,
       max(p.cosine) FILTER (WHERE NOT p.recent) AS before_cosine, b.n
  FROM busiest b JOIN pairs p ON p.tag = b.tag
 GROUP BY b.tag, b.n_recent, b.n_before, b.n
 ORDER BY b.n DESC, b.tag COLLATE "C"
"""

# One row per superseded entry that was flagged before its successor
# (written in the last 90 days) existed: seconds from its first standing
# stale/wrong verdict to the successor's ``created_at`` (ADR 0068's
# convention: a verdict written after the successor predicted nothing).
HEALTH_CORRECTIONS = f"""
SELECT extract(epoch FROM (n.created_at - min(f.updated_at)))::float8 AS seconds
  FROM entries n
  JOIN entries s ON s.superseded_by = n.id AND s.state = 'superseded'
  JOIN feedbacks f ON f.entry_id = s.id
                  AND f.verdict IN ('stale', 'wrong')
                  AND f.updated_at < n.created_at
                  AND {_RATING_COUNTS}
 WHERE n.created_at BETWEEN $1::timestamptz - interval '2160 hours' AND $1::timestamptz
 GROUP BY s.id, n.created_at
"""

# Audit log (ADR 0027): insert-only; the table has no UPDATE/DELETE path.
_AUDIT_COLUMNS = "id, occurred_at, actor_kind, actor, action, target, detail"
INSERT_AUDIT = f"""
INSERT INTO audit_log (actor_kind, actor, action, target, detail)
VALUES ($1, $2, $3, $4, $5::jsonb)
RETURNING {_AUDIT_COLUMNS}
"""


def _filter_conditions(
    filters: EntryFilters, visibility: Visibility | None = None
) -> tuple[list[str], list[Any]]:
    """Translate ``EntryFilters`` (+ optional ``Visibility``, ADR 0011)
    into WHERE-fragments and parameters.

    Mirrors ``EntryFilters.matches`` exactly (SPEC.md §5.3). ``visibility``
    ``None`` keeps the v1 flat pool.
    """
    clauses: list[str] = []
    params: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        params.append(value)
        clauses.append(fragment.replace("?", f"${len(params)}"))

    if not filters.include_inactive and not filters.relied_on_changed:
        clauses.append("state = 'active'")
    if filters.kind is not None:
        add("kind = ?", filters.kind.value)
    if filters.importance_source is not None:
        add("importance_source = ?", filters.importance_source.value)
    if filters.tags:
        add("tags @> ?", list(filters.tags))
    if filters.entities:
        # Case-insensitive AND over ``entity_names`` (ADR 0016, SPEC §13).
        add("entity_names @> ?", [name.lower() for name in filters.entities])
    if filters.scope is not None:
        add("scope = ?", filters.scope)
    if filters.fleet_id is not None:
        if is_valid_uuid(filters.fleet_id):
            add("fleet_id = ?", filters.fleet_id)
        else:
            clauses.append("FALSE")  # no such fleet, so no entry (as MemoryStore)
    if filters.author is not None:
        add("author = ?", filters.author)
    if filters.authors:
        add("author = ANY(?::text[])", list(filters.authors))
    if filters.agent is not None:
        add("agent = ?", filters.agent)
    if filters.occurred_from is not None:
        add("occurred_at >= ?", filters.occurred_from)
    if filters.occurred_to is not None:
        add("occurred_at <= ?", filters.occurred_to)
    if filters.created_from is not None:
        add("created_at >= ?", filters.created_from)
    if filters.created_to is not None:
        add("created_at <= ?", filters.created_to)

    if filters.flagged:
        # ADR 0054: at least one stale or wrong report. The feedbacks primary
        # key (entry_id, "user", agent) serves the per-entry probe.
        clauses.append(
            "EXISTS (SELECT 1 FROM feedbacks f WHERE f.entry_id = entries.id"
            " AND f.verdict IN ('stale', 'wrong') AND " + _RATING_COUNTS + ")"
        )

    if filters.relied_on_changed:
        # ADR 0098: the reporter's helpful row, and since its updated_at the
        # entry was withdrawn or superseded by someone else, or another
        # reporter filed stale/wrong. A withdrawal with no recorded time
        # (written by a pre-0016 pod) counts.
        if filters.reporter is None:
            clauses.append("FALSE")
        else:
            params.extend(filters.reporter)
            user_p, agent_p = f"${len(params) - 1}", f"${len(params)}"
            clauses.append(
                "EXISTS (SELECT 1 FROM feedbacks h WHERE h.entry_id = entries.id"
                f" AND h.\"user\" = {user_p} AND h.agent = {agent_p} AND h.verdict = 'helpful'"
                " AND h.set_aside_by IS NULL"
                " AND (entries.state = 'withdrawn'"
                " AND coalesce(entries.withdrawn_at > h.updated_at, TRUE)"
                " OR entries.state = 'superseded' AND EXISTS (SELECT 1 FROM entries s"
                ' WHERE s.id = entries.superseded_by AND s.author <> h."user"'
                " AND s.created_at > h.updated_at)"
                " OR EXISTS (SELECT 1 FROM feedbacks f WHERE f.entry_id = entries.id"
                " AND f.verdict IN ('stale', 'wrong') AND f.updated_at > h.updated_at"
                ' AND f."user" <> h."user"'
                " AND " + _RATING_COUNTS + ")))"
            )

    vis_clause = _visibility_clause(visibility, params)
    if vis_clause is not None:
        clauses.append(vis_clause)

    return clauses, params


def _visibility_clause(
    visibility: Visibility | None, params: list[Any], table: str = ""
) -> str | None:
    """The visibility clause for ``visibility`` (ADR 0011), or ``None``
    for no restriction; appends its parameters to ``params``.

    Mirrors ``domain.access.entry_is_visible``. ``table`` qualifies the
    ``entries`` columns (``"e."``) when the query joins another table.
    """
    if visibility is None or visibility.is_admin:
        return None
    if visibility.level == TrustLevel.UNTRUSTED:
        return "FALSE"

    def _p(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    if visibility.inherited:
        # ADR 0113: own = the caller's name or an agent that handed down to it.
        own = f"{table}author = ANY({_p(list(visibility.owned))}::text[])"
    else:
        own = f"{table}author = {_p(visibility.name)}"
    if visibility.level == TrustLevel.PRIVILEGED:
        # L3 reads every fleet's entries.
        return (
            f"({table}scope = 'org' OR ({table}scope = 'self' AND {own}) OR {table}scope = 'fleet')"
        )
    # L1/L2: fleet entries only if own or in the home fleet.
    home_p = _p(visibility.home_fleet_id)
    return (
        f"({table}scope = 'org' "
        f"OR ({table}scope = 'self' AND {own}) "
        f"OR ({table}scope = 'fleet' AND ({own} OR {table}fleet_id = {home_p})))"
    )


async def _refuse_merged(conn: asyncpg.Connection, fleet_id: str | None) -> None:
    """Share-lock ``fleet_id``'s row until the transaction ends and raise
    ``FleetMerged`` when the fleet was merged (ADR 0107). A fleet id that
    names no fleet passes, as before (``entries.fleet_id`` has no FK)."""
    if fleet_id is None or not is_valid_uuid(fleet_id):
        return
    # A merge holds the row for as long as it runs; give up after a few
    # seconds with a retryable answer rather than hold a connection.
    await conn.execute(f"SET LOCAL lock_timeout = '{WRITE_LOCK_TIMEOUT}'")
    try:
        row = await conn.fetchrow(GUARD_FLEET, fleet_id)
    except asyncpg.exceptions.LockNotAvailableError:
        raise FleetBusy(fleet_id) from None
    if row is not None and row["merged_into"] is not None:
        raise FleetMerged(fleet_id, str(row["merged_into"]), row["target_name"])


async def _refuse_stale_home(conn: asyncpg.Connection, fleet_id: str | None, author: str) -> None:
    """ADR 0112: a fleet move changes an agent's home fleet while it holds the
    fleets' rows, so a fleet write by ``author`` that resolved the old home
    before the move committed must not land in it now (``FleetBusy``, retry).
    Call after taking the fleet's share lock. An author that is no registered
    agent (the admin, a script) is not checked."""
    if fleet_id is None:
        return
    home = await conn.fetchrow(AGENT_HOME, author)
    if (
        home is not None
        and home["home_fleet_id"] is not None
        and str(home["home_fleet_id"]) != fleet_id
    ):
        raise FleetBusy(
            fleet_id,
            f"the home fleet of {author} changed during this write, retry in a few seconds",
        )


def _valid_uuids(ids: list[str] | tuple[str, ...]) -> list[str]:
    """Keep only the IDs that parse as UUIDs (others simply don't exist)."""
    return [i for i in ids if is_valid_uuid(i)]


class PgStore:
    """A ``Store`` backed by PostgreSQL + pgvector (ADR 0007).

    The pool opens lazily on first use; ``close()`` on shutdown. Pool
    sizes come from ``Settings.pool_min_size`` / ``pool_max_size``.
    """

    def __init__(
        self,
        dsn: str,
        *,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        timeouts: PoolTimeouts | None = None,
    ) -> None:
        self._dsn = dsn
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._timeouts = timeouts or PoolTimeouts()
        self._acquire_timeout = self._timeouts.acquire
        self._pool: asyncpg.Pool | None = None
        # Serialises lazy creation so concurrent first calls share one pool.
        self._pool_lock = asyncio.Lock()
        # Last database-probe outcome, so failures are logged once per
        # outage (with the cause) rather than on every probe (ADR 0037).
        self._database_reachable: bool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        """Lazily open (and cache) the connection pool — exactly once, even
        when the first calls arrive concurrently."""
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    self._pool = await make_pool(
                        self._dsn,
                        min_size=self._pool_min_size,
                        max_size=self._pool_max_size,
                        command_timeout=self._timeouts.command,
                        statement_timeout_ms=self._timeouts.statement_ms,
                        connect_timeout=self._timeouts.connect,
                    )
        return self._pool

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        """A pooled connection, acquired under the acquire timeout: a
        saturated pool raises ``TimeoutError`` (a 503 / ``store_unavailable``
        upstream) instead of waiting forever."""
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            yield conn

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """``_connection`` inside one transaction: committed when the block
        exits normally, rolled back when it raises."""
        async with self._connection() as conn, conn.transaction():
            yield conn

    async def close(self) -> None:
        """Tear down the pool (process-exit cleanup)."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # -- write path -------------------------------------------------------------

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

        async with self._transaction() as conn:
            await _refuse_merged(conn, draft.fleet_id)
            if draft.scope == "fleet":
                await _refuse_stale_home(conn, draft.fleet_id, draft.author)
            # RETURNING, not a re-read: the supersession flip below
            # never touches the new row, so this is its final state.
            row = await conn.fetchrow(
                INSERT_ENTRY_RETURNING,
                entry_id,
                draft.kind.value,
                draft.summary,
                draft.body,
                json.dumps(draft.payload) if draft.payload is not None else None,  # jsonb
                json.dumps(_encode_sources(draft.sources)),  # jsonb
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
                json.dumps(_encode_entities(entities)),  # jsonb
                [e.name.lower() for e in entities],  # lower-cased names: the filter column
                entities_model,  # ADR 0016: extractor model (provenance)
            )
            if draft.see_also:
                await conn.execute(INSERT_LINKS, entry_id, _valid_uuids(list(draft.see_also)))
            if draft.supersedes:
                # ADR 0034: any target the guarded UPDATE did not flip
                # aborts the whole transaction, insert included.
                wanted = list(dict.fromkeys(draft.supersedes))
                flipped = {
                    str(r["id"])
                    for r in await conn.fetch(FLIP_SUPERSEDED, entry_id, _valid_uuids(wanted))
                }
                denied = [t for t in wanted if canonical_uuid(t) not in flipped]
                if denied:
                    raise SupersedeConflict(denied)
        if row is None:
            raise RuntimeError(f"insert of entry {entry_id} returned no row")
        entry = _row_to_entry(row)
        if embedding is None:
            return entry
        # The writer gets its vector back; later reads do not fetch it.
        return replace(entry, embedding=tuple(float(x) for x in embedding))

    # -- read path ---------------------------------------------------------------

    async def get_entry(self, entry_id: str) -> Entry | None:
        if not is_valid_uuid(entry_id):
            return None
        async with self._connection() as conn:
            row = await conn.fetchrow(SELECT_ENTRY, entry_id)
        return _row_to_entry(row) if row is not None else None

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        ids = _valid_uuids(entry_ids)
        if not ids:
            return {}
        async with self._connection() as conn:
            rows = await conn.fetch(SELECT_ENTRIES, ids)
        return {str(row["id"]): _row_to_entry(row) for row in rows}

    async def pairwise_similarity(self, entry_ids: list[str]) -> dict[tuple[str, str], float]:
        ids = _valid_uuids(list(dict.fromkeys(entry_ids)))
        if len(ids) < 2:
            return {}
        async with self._connection() as conn:
            rows = await conn.fetch(PAIRWISE_SIMILARITY, ids)
        out: dict[tuple[str, str], float] = {}
        for row in rows:
            a, b = sorted((str(row["a"]), str(row["b"])))
            similarity = float(row["similarity"])
            # pgvector gives NaN for a zero vector; MemoryStore says 0.0.
            out[(a, b)] = similarity if math.isfinite(similarity) else 0.0
        return out

    async def list_predecessors(self, entry_ids: list[str]) -> list[Entry]:
        ids = _valid_uuids(entry_ids)
        if not ids:
            return []
        async with self._connection() as conn:
            rows = await conn.fetch(SELECT_PREDECESSORS, ids)
        return [_row_to_entry(row) for row in rows]

    async def entry_links(self, entry_id: str, limit: int) -> tuple[list[str], list[str]]:
        if not is_valid_uuid(entry_id):
            return [], []
        async with self._connection() as conn:
            outgoing = await conn.fetch(SELECT_LINKS_OUT, entry_id)
            incoming = await conn.fetch(SELECT_LINKS_IN, entry_id, limit)
        return [str(r["to_id"]) for r in outgoing], [str(r["from_id"]) for r in incoming]

    async def entry_links_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, tuple[list[str], list[str]]]:
        canonical = {eid: canonical_uuid(eid) for eid in entry_ids}
        ids = list(dict.fromkeys(c for c in canonical.values() if c is not None))
        links: dict[str, tuple[list[str], list[str]]] = {c: ([], []) for c in ids}
        if ids:
            async with self._connection() as conn:
                outgoing = await conn.fetch(SELECT_LINKS_OUT_MANY, ids)
                incoming = await conn.fetch(SELECT_LINKS_IN_MANY, ids, limit)
            for r in outgoing:
                links[str(r["from_id"])][0].append(str(r["to_id"]))
            for r in incoming:
                links[str(r["to_id"])][1].append(str(r["from_id"]))
        return {eid: links[c] if c is not None else ([], []) for eid, c in canonical.items()}

    async def upstream_links(
        self, entry_ids: list[str], *, visibility: Visibility | None = None
    ) -> dict[str, list[UpstreamLink]]:
        ids = _valid_uuids(entry_ids)
        if not ids:
            return {}
        params: list[Any] = [ids]
        vis = _visibility_clause(visibility, params)
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

    async def pin_entry(
        self,
        fleet_id: str,
        entry_id: str,
        pinned_by: str,
        limit: int,
        replaces: Sequence[str] = (),
        agent_limit: int | None = None,
        also_owned_by: Sequence[str] = (),
    ) -> PinWrite | PinRefusal:
        if not is_valid_uuid(entry_id):
            return PinRefusal.FLEET
        async with self._transaction() as conn:
            # The fleet row first, then the pin lock: the order a merge takes them.
            await _refuse_merged(conn, fleet_id)
            await _refuse_stale_home(conn, fleet_id, pinned_by)
            await conn.execute(LOCK_FLEET_PINS, fleet_id)
            # Decide before deleting anything: a refusal must leave the
            # pins as they were.
            fleet_pins = [_row_to_pin(r) for r in await conn.fetch(LIST_PINS, fleet_id)]
            refusal = pin_refusal(
                fleet_pins, entry_id, pinned_by, limit, agent_limit, replaces, also_owned_by
            )
            if refusal is not None:
                return refusal
            replaced = [
                old
                for old in dict.fromkeys(replaces)
                if old != entry_id
                and is_valid_uuid(old)
                and (await conn.execute(DELETE_PIN, fleet_id, old)).endswith(" 1")
            ]
            row = await conn.fetchrow(INSERT_PIN, fleet_id, entry_id, pinned_by)
        if row is None:
            raise RuntimeError("pin row vanished between insert and read")
        return PinWrite(_row_to_pin(row), bool(row["created"]), tuple(replaced))

    async def unpin_entry(self, fleet_id: str, entry_id: str) -> bool:
        if not is_valid_uuid(entry_id):
            return False
        async with self._connection() as conn:
            status = await conn.execute(DELETE_PIN, fleet_id, entry_id)
        return bool(status.endswith(" 1"))

    async def list_pins(self, fleet_id: str) -> list[Pin]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_PINS, fleet_id)
        return [_row_to_pin(row) for row in rows]

    async def usage_counts(self) -> list[UsageCount]:
        async with self._connection() as conn:
            rows = await conn.fetch(USAGE_COUNTS)
        return [
            UsageCount(
                scope=row["scope"],
                kind=Kind(row["kind"]),
                importance_source=ImportanceSource(row["importance_source"]),
                author=row["author"],
                fleet_id=str(row["fleet_id"]) if row["fleet_id"] is not None else None,
                active=bool(row["active"]),
                count=int(row["n"]),
            )
            for row in rows
        ]

    async def flag_counts(self) -> list[FlagCount]:
        async with self._connection() as conn:
            rows = await conn.fetch(FLAG_COUNTS)
        return [
            FlagCount(
                user=row["reporter"], flags=int(row["flags"]), confirmed=int(row["confirmed"])
            )
            for row in rows
        ]

    async def fleet_health(self, now: datetime) -> FleetHealth:
        # One read-only REPEATABLE READ transaction: the four statements share
        # a snapshot, so the figures agree with each other. They run one after
        # the other (a connection runs one statement at a time); JIT is off, as
        # compiling these short queries costs more than it saves.
        async with (
            self._connection() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL jit = off")
            by_fleet = await conn.fetch(HEALTH_BY_FLEET, now)
            scalars = await conn.fetchrow(HEALTH_SCALARS, now, ENTRY_ID_REF)
            corrections = await conn.fetch(HEALTH_CORRECTIONS, now)
            rivals = await conn.fetch(HEALTH_RIVALS, now)
        assert scalars is not None
        return FleetHealth(
            by_fleet=tuple(
                HealthCounts(
                    fleet_id=str(r["fleet_id"]) if r["fleet_id"] is not None else None,
                    sourceable=int(r["sourceable"]),
                    sourced=int(r["sourced"]),
                    insights=int(r["insights"]),
                    synthesizing=int(r["synthesizing"]),
                    flagged_backlog_14d=int(r["backlog"]),
                    orphaned_flagged=int(r["orphaned"]),
                    floating_gap=int(r["floating"]),
                    gap_anomaly=int(r["gap_anomaly"]),
                    gap_open_question=int(r["gap_open_question"]),
                    gap_dead_end=int(r["gap_dead_end"]),
                )
                for r in by_fleet
            ),
            helpful=int(scalars["helpful"]),
            helpful_with_note=int(scalars["helpful_noted"]),
            correction_latencies=tuple(float(r["seconds"]) for r in corrections),
            writes_90d=int(scalars["writes"]),
            top3_author_writes_90d=int(scalars["top_authors"]),
            authors_90d=int(scalars["authors"]),
            verdicts_90d=int(scalars["verdicts"]),
            top3_reporter_verdicts_90d=int(scalars["top_reporters"]),
            reporters_90d=int(scalars["reporters"]),
            sibling_verdicts_90d=int(scalars["sibling_verdicts"]),
            top3_owner_verdicts_90d=int(scalars["top_owners"]),
            owners_90d=int(scalars["owners"]),
            supersessions_90d=int(scalars["supersessions"]),
            self_to_fleet_promotions_90d=int(scalars["promotions"]),
            alternative_ages=_ages(rivals, "alternative"),
            conflict_ages=_ages(rivals, "conflict"),
            contested_ages=_ages(rivals, "contested"),
            synthesis_writes_30d=int(scalars["synthesis_writes"]),
            grounded_30d=int(scalars["grounded"]),
            writes_30d=int(scalars["writes30"]),
            top3_label_writes_30d=int(scalars["top_write_labels"]),
            write_labels_30d=int(scalars["write_labels"]),
            verdicts_30d=int(scalars["verdicts30"]),
            top3_label_verdicts_30d=int(scalars["top_verdict_labels"]),
            verdict_labels_30d=int(scalars["verdict_labels"]),
        )

    async def activity(self, since: datetime) -> Activity:
        # One read-only REPEATABLE READ transaction, like ``fleet_health``:
        # the four scans share a snapshot, so the figures agree.
        async with (
            self._connection() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            await conn.execute("SET LOCAL jit = off")
            authors = await conn.fetch(ACTIVITY_AUTHORS, since)
            reporters = await conn.fetch(ACTIVITY_REPORTERS, since)
            received = await conn.fetch(ACTIVITY_RECEIVED)
            days = await conn.fetch(ACTIVITY_DAYS, since)
        return Activity(
            authors=tuple(
                AuthorActivity(
                    author=r["author"],
                    fleet_id=str(r["fleet_id"]) if r["fleet_id"] is not None else None,
                    entries=int(r["entries"]),
                    active=int(r["active"]),
                    recent=int(r["recent"]),
                    last_write_at=r["last_write_at"],
                )
                for r in authors
            ),
            reporters=tuple(
                ReporterActivity(
                    reporter=r["reporter"],
                    verdicts=int(r["verdicts"]),
                    recent=int(r["recent"]),
                    helpful=int(r["helpful"]),
                    stale=int(r["stale"]),
                    wrong=int(r["wrong"]),
                    last_at=r["last_at"],
                )
                for r in reporters
            ),
            received=tuple(
                ReceivedFeedback(
                    author=r["author"],
                    helpful=int(r["helpful"]),
                    stale=int(r["stale"]),
                    wrong=int(r["wrong"]),
                )
                for r in received
            ),
            days=tuple(
                DayActivity(
                    day=r["day"],
                    writes=int(r["writes"]),
                    helpful=int(r["helpful"]),
                    stale=int(r["stale"]),
                    wrong=int(r["wrong"]),
                )
                for r in days
            ),
        )

    async def fleet_convergence(self, now: datetime) -> Convergence:
        async with self._transaction() as conn:
            await conn.execute("SET LOCAL jit = off")
            await conn.execute(VECTOR_SEARCH_SETTINGS)
            echo = await conn.fetchrow(CONVERGENCE_ECHO, now, ECHO_SAMPLE, ECHO_THRESHOLD)
            rows = await conn.fetch(
                CONVERGENCE_DISPERSION, now, DISPERSION_CAP, DISPERSION_TAGS, DISPERSION_MIN
            )
        assert echo is not None
        return Convergence(
            echo_sampled=int(echo["sampled"]),
            echo_hits=int(echo["echoes"]),
            dispersion=tuple(
                TagDispersion(
                    tag=row["tag"],
                    recent_entries=int(row["n_recent"]),
                    before_entries=int(row["n_before"]),
                    recent_cosine=float(row["recent_cosine"]),
                    before_cosine=float(row["before_cosine"]),
                )
                for row in rows
            ),
        )

    async def record_search(
        self, fleet_id: str | None, *, empty: bool, explore: bool = False
    ) -> None:
        async with self._connection() as conn:
            await conn.execute(RECORD_SEARCH, fleet_id or "", int(empty), int(explore))

    async def search_counts(self) -> list[SearchCount]:
        async with self._connection() as conn:
            rows = await conn.fetch(SEARCH_COUNTS)
        return [
            SearchCount(
                fleet_id=row["fleet_id"] or None,
                searches=int(row["searches"]),
                empty=int(row["empty"]),
                explore=int(row["explore"]),
            )
            for row in rows
        ]

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
        clauses, params = _filter_conditions(filters, visibility)
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
        return [_row_to_entry(row) for row in rows]

    async def count_entries(
        self, filters: EntryFilters, *, visibility: Visibility | None = None
    ) -> int:
        """Count entries matching ``filters`` with a SQL ``COUNT``;
        ``visibility`` as in ``list_entries``."""
        clauses, params = _filter_conditions(filters, visibility)
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
        return _row_to_entry(row)

    # -- search -------------------------------------------------------------------

    async def search_keyword(
        self,
        query: str,
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
    ) -> list[str]:
        """Ranked entry IDs by ``ts_rank`` (SPEC.md §6.2 keyword stream).
        Any query term matches; see ``ANY_TERM_TSQUERY``."""
        clauses, params = _filter_conditions(filters, visibility)
        query_idx = len(params) + 1
        tsquery = ANY_TERM_TSQUERY.format(param=f"${query_idx}")
        clauses.append(f"search_tsv @@ {tsquery}")
        params.append(query)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = (
            "SELECT id FROM entries WHERE "
            + where
            + f" ORDER BY ts_rank(search_tsv, {tsquery}) DESC,"
            + " created_at DESC, id DESC"
            + f" LIMIT ${query_idx + 1}"
        )
        async with self._connection() as conn:
            rows = await conn.fetch(sql, *params, limit)
        return [str(r["id"]) for r in rows]

    async def search_vector(
        self,
        embedding: list[float],
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
        min_similarity: float = 0.0,
    ) -> list[str]:
        """Ranked entry IDs by cosine distance (SPEC.md §6.2 vector stream).

        Approximate: served by the HNSW index (ADR 0025), under
        ``VECTOR_SEARCH_SETTINGS`` applied in the same transaction.
        Entries with similarity <= ``min_similarity`` are left out (ADRs
        0061, 0062). They sort last, so dropping them from the top
        ``limit`` rows equals dropping them first, and the index scan is
        the same as without the cut-off.
        """
        clauses, params = _filter_conditions(filters, visibility)
        clauses.append("embedding IS NOT NULL")
        vec_idx = len(params) + 1
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = (
            f"SELECT id, embedding <=> ${vec_idx} AS distance FROM entries WHERE "
            + where
            + f" ORDER BY embedding <=> ${vec_idx}"
            + f" LIMIT ${vec_idx + 1}"
        )
        async with self._transaction() as conn:
            await conn.execute(VECTOR_SEARCH_SETTINGS)
            rows = await conn.fetch(sql, *params, embedding, limit)
        return [str(r["id"]) for r in rows if 1.0 - r["distance"] > min_similarity]

    async def similar_entries(
        self,
        embedding: list[float],
        limit: int,
        *,
        exclude_id: str | None = None,
        visibility: Visibility | None = None,
    ) -> list[tuple[str, float]]:
        """The active entries nearest to ``embedding`` with their cosine
        similarity (ADR 0052). The same index, filters and ``SET LOCAL``
        settings as ``search_vector``, so it is approximate too (ADR 0025)."""
        clauses, params = _filter_conditions(EntryFilters(), visibility)
        clauses.append("embedding IS NOT NULL")
        if exclude_id is not None and is_valid_uuid(exclude_id):
            params.append(exclude_id)
            clauses.append(f"id <> ${len(params)}")
        vec_idx = len(params) + 1
        sql = (
            f"SELECT id, 1 - (embedding <=> ${vec_idx}) AS similarity FROM entries WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY embedding <=> ${vec_idx}"
            + f" LIMIT ${vec_idx + 1}"
        )
        async with self._transaction() as conn:
            await conn.execute(VECTOR_SEARCH_SETTINGS)
            rows = await conn.fetch(sql, *params, embedding, limit)
        return [(str(r["id"]), float(r["similarity"])) for r in rows]

    # -- feedback -----------------------------------------------------------------

    async def record_feedback(self, feedback: Feedback) -> Feedback:
        """Upsert a feedback verdict (one row per entry+user+agent)."""
        async with self._connection() as conn:
            updated_at = await conn.fetchval(
                UPSERT_FEEDBACK,
                feedback.entry_id,
                feedback.user,
                feedback.agent,
                feedback.verdict.value,
                feedback.note,
                feedback.updated_at,
            )
        return replace(feedback, updated_at=updated_at)

    async def record_feedback_many(self, feedbacks: list[Feedback]) -> list[Feedback]:
        """Upsert several feedback rows in one transaction: all land or
        none do."""
        if not feedbacks:
            return []
        async with self._transaction() as conn:
            # ``now()`` is the transaction's start time, so this is the
            # stamp every row without its own ``updated_at`` gets.
            now = await conn.fetchval("SELECT now()")
            await conn.executemany(
                UPSERT_FEEDBACK,
                [
                    (f.entry_id, f.user, f.agent, f.verdict.value, f.note, f.updated_at)
                    for f in feedbacks
                ],
            )
        return [replace(f, updated_at=f.updated_at or now) for f in feedbacks]

    async def feedback_counts(self, entry_id: str) -> tuple[int, int, int]:
        if not is_valid_uuid(entry_id):
            return (0, 0, 0)
        async with self._connection() as conn:
            row = await conn.fetchrow(COUNT_FEEDBACK, entry_id)
        return (row["helpful"], row["stale"], row["wrong"]) if row else (0, 0, 0)

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, tuple[int, int, int]]:
        """Batched feedback counts; every valid requested ID is present."""
        ids = _valid_uuids(entry_ids)
        if not ids:
            return {}
        async with self._connection() as conn:
            rows = await conn.fetch(BATCH_FEEDBACK, ids)
        counts = dict.fromkeys(ids, (0, 0, 0))
        for r in rows:
            counts[str(r["entry_id"])] = (r["helpful"], r["stale"], r["wrong"])
        return counts

    async def feedback_signals(
        self,
        entry_ids: list[str],
        *,
        count_supersedes: bool = False,
        visibility: Visibility | None = None,
    ) -> dict[str, FeedbackSignals]:
        """Batched counts, last-confirmed dates and corrections (one
        statement), plus the visibility-aware ``supersedes`` counts (a second
        statement on the same connection) when asked; every valid requested ID
        is present."""
        ids = _valid_uuids(entry_ids)
        if not ids:
            return {}
        supersedes: dict[str, int] = {}
        async with self._connection() as conn:
            rows = await conn.fetch(BATCH_FEEDBACK_SIGNALS, ids)
            if count_supersedes:
                params: list[Any] = [ids]
                vis = _visibility_clause(visibility, params)
                sql = COUNT_SUPERSEDES.format(vis=f" AND {vis}" if vis else "")
                supersedes = {str(r["entry_id"]): r["n"] for r in await conn.fetch(sql, *params)}
        signals = {eid: FeedbackSignals(0, 0, 0, supersedes=supersedes.get(eid, 0)) for eid in ids}
        for r in rows:
            eid = str(r["entry_id"])
            correction = (
                Correction(
                    Verdict(r["corr_verdict"]),
                    truncate_note(r["corr_note"]),
                    r["corr_user"],
                    r["corr_updated_at"],
                )
                if r["corr_updated_at"] is not None
                else None
            )
            signals[eid] = FeedbackSignals(
                r["helpful"],
                r["stale"],
                r["wrong"],
                last_confirmed(r["newest_noted_helpful_at"], r["newest_flag_at"]),
                correction,
                supersedes.get(eid, 0),
                r["newest_flag_at"],
            )
        return signals

    async def list_feedback(self, entry_id: str, limit: int) -> list[Feedback]:
        """An entry's newest feedback rows. The primary key
        ``(entry_id, "user", agent)`` narrows the scan to one entry's rows."""
        if not is_valid_uuid(entry_id):
            return []
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_FEEDBACK, entry_id, limit)
        return [_row_to_feedback(r) for r in rows]

    async def list_feedback_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, list[Feedback]]:
        canonical = {eid: canonical_uuid(eid) for eid in entry_ids}
        ids = list(dict.fromkeys(c for c in canonical.values() if c is not None))
        rows_by_id: dict[str, list[Feedback]] = {c: [] for c in ids}
        if ids:
            async with self._connection() as conn:
                rows = await conn.fetch(LIST_FEEDBACK_MANY, ids, limit)
            for r in rows:
                rows_by_id[str(r["entry_id"])].append(_row_to_feedback(r))
        return {eid: rows_by_id[c] if c is not None else [] for eid, c in canonical.items()}

    async def list_feedback_log(
        self,
        filters: FeedbackLogFilters,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FeedbackLogItem]:
        """Feedback rows across entries, newest first. No index serves the
        order: a scan of ``feedbacks`` and a top-N sort, read by the admin
        panel only."""
        clauses = [_RATING_COUNTS]
        params: list[Any] = []

        def add(fragment: str, value: Any) -> None:
            params.append(value)
            clauses.append(fragment.replace("?", f"${len(params)}"))

        for value, fragment in (
            (filters.entry_id, "f.entry_id = ?"),
            (filters.fleet_id, "e.fleet_id = ?"),
        ):
            if value is None:
                continue
            if is_valid_uuid(value):
                add(fragment, value)
            else:
                clauses.append("FALSE")
        if filters.reporter is not None:
            add('f."user" = ?', filters.reporter)
        if filters.verdict is not None:
            add("f.verdict = ?", filters.verdict.value)
        if filters.author is not None:
            add("e.author = ?", filters.author)
        if filters.authors:
            add("e.author = ANY(?::text[])", list(filters.authors))
        vis = _visibility_clause(visibility, params, "e.")
        if vis is not None:
            clauses.append(vis)
        sql = FEEDBACK_LOG.format(
            where=" AND ".join(clauses), limit=len(params) + 1, offset=len(params) + 2
        )
        async with self._connection() as conn:
            rows = await conn.fetch(sql, *params, limit, offset)
        return [
            FeedbackLogItem(
                feedback=_row_to_feedback(r),
                entry_kind=Kind(r["kind"]),
                entry_summary=r["summary"],
                entry_author=r["author"],
                entry_scope=r["scope"],
                entry_fleet_id=str(r["fleet_id"]) if r["fleet_id"] is not None else None,
                entry_state=EntryState(r["state"]),
            )
            for r in rows
        ]

    async def list_flag_backlog(
        self,
        filters: FlagBacklogFilters,
        now: datetime,
        limit: int,
        offset: int = 0,
        *,
        visibility: Visibility | None = None,
    ) -> list[FlaggedEntry]:
        """The flag backlog, oldest first flag first. Every standing flag is
        grouped before the page is cut: flagged entries are few, and only
        the admin panel reads this."""
        params: list[Any] = [now]
        inner = ["TRUE"]
        outer = ["TRUE"]
        if filters.fleet_id is not None:
            if is_valid_uuid(filters.fleet_id):
                params.append(filters.fleet_id)
                inner.append(f"e.fleet_id = ${len(params)}")
            else:
                inner.append("FALSE")
        if filters.authors:
            params.append(list(filters.authors))
            inner.append(f"e.author = ANY(${len(params)}::text[])")
        vis = _visibility_clause(visibility, params, "e.")
        if vis is not None:
            inner.append(vis)
        if filters.flagged_before is not None:
            params.append(filters.flagged_before)
            outer.append(f"first_flag < ${len(params)}")
        if filters.orphaned:
            outer.append("orphaned")
        sql = FLAG_BACKLOG.format(
            inner=" AND ".join(inner),
            outer=" AND ".join(outer),
            limit=len(params) + 1,
            offset=len(params) + 2,
        )
        async with self._connection() as conn:
            rows = await conn.fetch(sql, *params, limit, offset)
        return [
            FlaggedEntry(
                entry_id=str(r["id"]),
                kind=Kind(r["kind"]),
                summary=r["summary"],
                author=r["author"],
                scope=r["scope"],
                fleet_id=str(r["fleet_id"]) if r["fleet_id"] is not None else None,
                first_flag_at=r["first_flag"],
                last_flag_at=r["last_flag"],
                helpful=int(r["helpful"]),
                stale=int(r["stale"]),
                wrong=int(r["wrong"]),
                orphaned=bool(r["orphaned"]),
            )
            for r in rows
        ]

    # -- fleets (ADR 0011) ------------------------------------------------------

    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet. ``FleetExists`` if the name
        exists, including a racing duplicate (conflict-guarded insert)."""
        async with self._connection() as conn:
            dup = await conn.fetchrow(SELECT_FLEET_BY_NAME, name)
            if dup is not None:
                raise FleetExists(f"fleet already exists: {name!r}")
            row = await conn.fetchrow(INSERT_FLEET, name)
            if row is None:
                # A concurrent create won the race.
                raise FleetExists(f"fleet already exists: {name!r}")
        return _row_to_fleet(row)

    async def list_fleets(self) -> list[Fleet]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_FLEETS)
        return [_row_to_fleet(r) for r in rows]

    async def get_fleet(self, fleet_id: str) -> Fleet | None:
        if not is_valid_uuid(fleet_id):
            return None
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_FLEET, fleet_id)
        return _row_to_fleet(row) if row is not None else None

    async def rename_fleet(self, fleet_id: str, name: str) -> tuple[Fleet, Fleet]:
        """Rename a fleet (ADR 0109); see ``Store.rename_fleet``. The row is
        locked so the returned ``before`` is the name the audit row reports;
        a racing rename to the same name loses on the unique index."""
        if not is_valid_uuid(fleet_id):
            raise UnknownFleet(f"unknown fleet: {fleet_id}")
        try:
            async with self._transaction() as conn:
                row = await conn.fetchrow(GET_FLEET_FOR_UPDATE, fleet_id)
                if row is None:
                    raise UnknownFleet(f"unknown fleet: {fleet_id}")
                before = _row_to_fleet(row)
                if before.name == name:
                    return before, before
                if await conn.fetchrow(SELECT_FLEET_BY_NAME, name) is not None:
                    raise FleetExists(f"fleet already exists: {name!r}")
                renamed = await conn.fetchrow(RENAME_FLEET, fleet_id, name)
        except asyncpg.UniqueViolationError as exc:
            raise FleetExists(f"fleet already exists: {name!r}") from exc
        assert renamed is not None  # the row is locked above
        return before, _row_to_fleet(renamed)

    # -- fleet merge (ADR 0107) ---------------------------------------------------

    async def fleet_facts(self, fleet_id: str) -> FleetFacts:
        if not is_valid_uuid(fleet_id):
            return FleetFacts(agents=0, entries={})
        async with self._connection() as conn:
            agents = await conn.fetchval(FLEET_AGENT_COUNT, fleet_id)
            rows = await conn.fetch(FLEET_ENTRY_STATES, fleet_id)
        return FleetFacts(agents=int(agents), entries={r["state"]: int(r["n"]) for r in rows})

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
                    rows = await conn.fetch(DUPLICATE_PAIRS, fleet_id, other_id, looked, threshold)
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
        if (target_fleet_id is None) == (new_fleet_name is None):
            raise InvalidInput("name exactly one of target_fleet_id and new_fleet_name")
        for given in (source_id, target_fleet_id):
            if given is not None and not is_valid_uuid(given):
                raise UnknownFleet(f"unknown fleet: {given}")
        source_id = str(uuid.UUID(source_id))
        if target_fleet_id is not None:
            target_fleet_id = str(uuid.UUID(target_fleet_id))
            if target_fleet_id == source_id:
                raise FleetMergeRefused("a fleet cannot be merged into itself")
        try:
            async with self._transaction() as conn:
                # Give up rather than queue behind in-flight writes (the migrations' pattern).
                await conn.execute(MERGE_SETTINGS)
                created = target_fleet_id is None
                if target_fleet_id is None:
                    # An unknown source wins over a taken name (as in MemoryStore).
                    if await conn.fetchrow(GET_FLEET, source_id) is None:
                        raise UnknownFleet(f"unknown fleet: {source_id}")
                    if await conn.fetchrow(SELECT_FLEET_BY_NAME, new_fleet_name) is not None:
                        raise FleetExists(f"fleet already exists: {new_fleet_name!r}")
                    made = await conn.fetchrow(INSERT_FLEET, new_fleet_name)
                    if made is None:
                        raise FleetExists(f"fleet already exists: {new_fleet_name!r}")
                    target_fleet_id = str(made["id"])
                locked = {
                    str(r["id"]): r
                    for r in await conn.fetch(LOCK_FLEETS, [source_id, target_fleet_id])
                }
                for fid in (source_id, target_fleet_id):
                    if fid not in locked:
                        raise UnknownFleet(f"unknown fleet: {fid}")
                for fid in (source_id, target_fleet_id):
                    row = locked[fid]
                    if row["merged_into"] is not None:
                        raise FleetMerged(fid, str(row["merged_into"]), row["target_name"])
                # Pins last-but-one in the lock order: fleet rows, then pin locks.
                for fid in sorted((source_id, target_fleet_id)):
                    await conn.execute(LOCK_FLEET_PINS, fid)
                pins = [
                    _row_to_pin(r)
                    for r in await conn.fetch(PINS_OF_FLEETS, [source_id, target_fleet_id])
                ]
                check_pins_unchanged(pins, expected_pin_ids)
                kept, dropped = choose_pins(pins, keep_pin_ids)

                merge_id = str(uuid.uuid4())
                moved = [
                    str(r["id"])
                    for r in await conn.fetch(
                        MOVE_ENTRIES, source_id, target_fleet_id, timeout=LONG_OP_TIMEOUT
                    )
                ]
                if moved:
                    await conn.execute(
                        RECORD_MERGED_ENTRIES, merge_id, moved, timeout=LONG_OP_TIMEOUT
                    )
                agents = sorted(
                    r["name"] for r in await conn.fetch(MOVE_AGENTS, source_id, target_fleet_id)
                )
                for fid in (source_id, target_fleet_id):
                    gone = [p.entry_id for p in dropped if p.fleet_id == fid]
                    if gone:
                        await conn.execute(DELETE_PINS_OF, fid, gone)
                await conn.execute(REKEY_PINS, source_id, target_fleet_id)
                await conn.execute(FOLD_SEARCH_COUNTS, source_id, target_fleet_id)
                await conn.execute(DELETE_SEARCH_COUNTS, source_id)
                merged_at = await conn.fetchval(MARK_MERGED, source_id, target_fleet_id)
                drops = tuple(
                    DroppedPin(p.fleet_id, p.entry_id, p.pinned_by, p.pinned_at) for p in dropped
                )
                await conn.execute(
                    INSERT_MERGE,
                    merge_id,
                    source_id,
                    target_fleet_id,
                    merged_by,
                    merged_at,
                    agents,
                    _dropped_json(drops),
                    KIND_MERGE,
                )
                target_row = locked[target_fleet_id]
        except asyncpg.exceptions.LockNotAvailableError:
            raise FleetMergeRefused(
                "the fleets are busy with writes; try the merge again in a moment"
            ) from None
        except asyncpg.exceptions.DeadlockDetectedError:
            raise FleetMergeRefused(
                "the merge deadlocked with a concurrent change; try it again"
            ) from None
        record = FleetMergeRecord(
            merge_id, source_id, target_fleet_id, merged_by, merged_at, tuple(agents), drops
        )
        target = _row_to_fleet(target_row)
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
        if (target_fleet_id is None) == (new_fleet_name is None):
            raise InvalidInput("name exactly one of target_fleet_id and new_fleet_name")
        names = sorted(set(agents))
        if not names:
            raise InvalidInput("name at least one agent to move")
        for given in (source_id, target_fleet_id):
            if given is not None and not is_valid_uuid(given):
                raise UnknownFleet(f"unknown fleet: {given}")
        source_id = str(uuid.UUID(source_id))
        if target_fleet_id is not None:
            target_fleet_id = str(uuid.UUID(target_fleet_id))
            if target_fleet_id == source_id:
                raise FleetMoveRefused("agents cannot be moved into the fleet they are in")
        try:
            async with self._transaction() as conn:
                await conn.execute(MERGE_SETTINGS)
                created = target_fleet_id is None
                if target_fleet_id is None:
                    if await conn.fetchrow(GET_FLEET, source_id) is None:
                        raise UnknownFleet(f"unknown fleet: {source_id}")
                    if await conn.fetchrow(SELECT_FLEET_BY_NAME, new_fleet_name) is not None:
                        raise FleetExists(f"fleet already exists: {new_fleet_name!r}")
                    made = await conn.fetchrow(INSERT_FLEET, new_fleet_name)
                    if made is None:
                        raise FleetExists(f"fleet already exists: {new_fleet_name!r}")
                    target_fleet_id = str(made["id"])
                # Both fleet rows FOR UPDATE, in id order, as a merge does: a fleet
                # write waits, so no entry can land in the source after the list below.
                locked = {
                    str(r["id"]): r
                    for r in await conn.fetch(LOCK_FLEETS, [source_id, target_fleet_id])
                }
                for fid in (source_id, target_fleet_id):
                    if fid not in locked:
                        raise UnknownFleet(f"unknown fleet: {fid}")
                for fid in (source_id, target_fleet_id):
                    row = locked[fid]
                    if row["merged_into"] is not None:
                        raise FleetMerged(fid, str(row["merged_into"]), row["target_name"])
                homes = {
                    r["name"]: r["home_fleet_id"] for r in await conn.fetch(LOCK_AGENTS, names)
                }
                away = [n for n in names if str(homes.get(n)) != source_id]
                if away:
                    raise FleetMoveRefused(
                        f"not agents of fleet {locked[source_id]['name']!r} (or unknown): "
                        + ", ".join(away)
                    )
                for fid in sorted((source_id, target_fleet_id)):
                    await conn.execute(LOCK_FLEET_PINS, fid)
                await conn.fetch(LOCK_MOVER_ENTRIES, source_id, names, timeout=LONG_OP_TIMEOUT)
                moving_rows = await conn.fetch(PLAN_MOVE, source_id, names, timeout=LONG_OP_TIMEOUT)
                moving = [str(r["id"]) for r in moving_rows]
                total = await conn.fetchval(
                    COUNT_MOVER_ENTRIES, source_id, names, timeout=LONG_OP_TIMEOUT
                )
                held = int(total) - len(moving)
                pins = [
                    _row_to_pin(r)
                    for r in await conn.fetch(
                        MOVE_PIN_CANDIDATES, source_id, target_fleet_id, moving
                    )
                ]
                check_pins_unchanged(pins, expected_pin_ids, FleetMoveRefused)
                kept, dropped = choose_pins(pins, keep_pin_ids)

                move_id = str(uuid.uuid4())
                if moving:
                    await conn.execute(
                        MOVE_THESE_ENTRIES,
                        source_id,
                        target_fleet_id,
                        moving,
                        timeout=LONG_OP_TIMEOUT,
                    )
                    await conn.execute(
                        RECORD_MERGED_ENTRIES, move_id, moving, timeout=LONG_OP_TIMEOUT
                    )
                for fid in (source_id, target_fleet_id):
                    gone = [p.entry_id for p in dropped if p.fleet_id == fid]
                    if gone:
                        await conn.execute(DELETE_PINS_OF, fid, gone)
                await conn.execute(REKEY_THESE_PINS, source_id, target_fleet_id, moving)
                await conn.fetch(MOVE_THESE_AGENTS, names, target_fleet_id, source_id)
                moved_at = await conn.fetchval("SELECT clock_timestamp()")
                drops = tuple(
                    DroppedPin(p.fleet_id, p.entry_id, p.pinned_by, p.pinned_at) for p in dropped
                )
                await conn.execute(
                    INSERT_MERGE,
                    move_id,
                    source_id,
                    target_fleet_id,
                    moved_by,
                    moved_at,
                    names,
                    _dropped_json(drops),
                    KIND_MOVE,
                )
                target_row = locked[target_fleet_id]
        except asyncpg.exceptions.LockNotAvailableError:
            raise FleetMoveRefused(
                "the fleets are busy with writes; try the move again in a moment"
            ) from None
        except asyncpg.exceptions.DeadlockDetectedError:
            raise FleetMoveRefused(
                "the move deadlocked with a concurrent change; try it again"
            ) from None
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
            _row_to_fleet(target_row),
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
        try:
            async with self._transaction() as conn:
                await conn.execute(MERGE_SETTINGS)
                row = await conn.fetchrow(GET_MERGE, merge_id)
                if row is None:
                    raise UnknownFleetMerge(f"unknown fleet merge: {merge_id}")
                record = _row_to_merge(row)
                src, tgt = record.source_fleet_id, record.target_fleet_id
                locked = {str(r["id"]): r for r in await conn.fetch(LOCK_FLEETS, [src, tgt])}
                involving = [
                    _row_to_merge(r) for r in await conn.fetch(MERGES_INVOLVING, [src, tgt])
                ]
                # Re-read under the locks: a racing merge or undo has finished by now.
                record = next((m for m in involving if m.id == merge_id), record)
                reason = undo_refusal(
                    record,
                    involving,
                    _row_to_fleet(locked[tgt]) if tgt in locked else None,
                    _row_to_fleet(locked[src]) if src in locked else None,
                )
                if reason is not None:
                    raise (FleetMoveRefused if record.kind == KIND_MOVE else FleetMergeRefused)(
                        reason
                    )
                for fid in sorted((src, tgt)):
                    await conn.execute(LOCK_FLEET_PINS, fid)
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
                restored: list[DroppedPin] = []
                skipped: list[DroppedPin] = []
                unpinned: list[DroppedPin] = []
                is_move = record.kind == KIND_MOVE
                if is_move:
                    # A move's pins that come back face the source's cap like any
                    # restored pin (the source may have refilled its briefing).
                    returning = [
                        DroppedPin(
                            str(r["fleet_id"]), str(r["entry_id"]), r["pinned_by"], r["pinned_at"]
                        )
                        for r in await conn.fetch(MOVE_RETURNING_PINS, tgt, back)
                    ]
                    mine = [d for d in record.dropped_pins if d.fleet_id == src]
                    held = await conn.fetch(PINNED_ENTRY_IDS, src)
                    pinned = {str(r["entry_id"]) for r in held}
                    home = {
                        str(r["id"])
                        for r in await conn.fetch(ENTRIES_IN_FLEET, [d.entry_id for d in mine], src)
                    }
                    back_pinned = {d.entry_id for d in returning}  # already coming back
                    fits = [
                        d
                        for d in mine
                        if d.entry_id in home
                        and d.entry_id not in pinned
                        and d.entry_id not in back_pinned
                    ]
                    skipped.extend(
                        d for d in mine if d not in fits and d.entry_id not in back_pinned
                    )
                    put, over = plan_pin_restore(
                        [*returning, *fits], [r["pinned_by"] for r in held]
                    )
                    for d in put:
                        if d in returning:
                            await conn.execute(MOVE_PIN_HOME, tgt, src, d.entry_id)
                        else:
                            await conn.execute(
                                RESTORE_PIN, src, d.entry_id, d.pinned_by, d.pinned_at
                            )
                            restored.append(d)
                    for d in over:
                        if d in returning:
                            await conn.execute(DELETE_PINS_OF, tgt, [d.entry_id])
                            unpinned.append(d)
                        skipped.append(d)
                    returned_count = sum(1 for d in put if d in returning)
                else:
                    returned_count = len(await conn.fetch(RETURN_PINS, merge_id, src, tgt))
                for fid in (tgt,) if is_move else (src, tgt):
                    mine = [d for d in record.dropped_pins if d.fleet_id == fid]
                    if not mine:
                        continue
                    home = {
                        str(r["id"])
                        for r in await conn.fetch(ENTRIES_IN_FLEET, [d.entry_id for d in mine], fid)
                    }
                    held = await conn.fetch(PINNED_ENTRY_IDS, fid)
                    pinned = {str(r["entry_id"]) for r in held}
                    fits = [d for d in mine if d.entry_id in home and d.entry_id not in pinned]
                    skipped.extend(d for d in mine if d not in fits)
                    put, over = plan_pin_restore(fits, [r["pinned_by"] for r in held])
                    skipped.extend(over)
                    for d in put:
                        await conn.execute(RESTORE_PIN, fid, d.entry_id, d.pinned_by, d.pinned_at)
                    restored.extend(put)
                if record.kind != KIND_MOVE:  # a move never marked its source
                    await conn.execute(CLEAR_MERGED, src)
                undone_at = await conn.fetchval(MARK_UNDONE, merge_id)
        except asyncpg.exceptions.LockNotAvailableError:
            raise FleetMergeRefused(
                "the fleets are busy with writes; try the undo again in a moment"
            ) from None
        except asyncpg.exceptions.DeadlockDetectedError:
            raise FleetMergeRefused(
                "the undo deadlocked with a concurrent change; try it again"
            ) from None
        return UnmergeOutcome(
            replace(record, undone_at=undone_at),
            len(back),
            tuple(agents),
            returned_count,
            tuple(restored),
            tuple(skipped),
            tuple(unpinned),
        )

    async def list_fleet_merges(self, limit: int) -> list[FleetMergeRecord]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_MERGES, limit)
        return [_row_to_merge(r) for r in rows]

    async def get_fleet_merge(self, merge_id: str) -> FleetMergeRecord | None:
        if not is_valid_uuid(merge_id):
            return None
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_MERGE, merge_id)
        return _row_to_merge(row) if row is not None else None

    # -- agent cleanup (ADR 0111) -------------------------------------------------

    async def scan_agent_cleanup(self, agent: str, since: datetime, until: datetime) -> CleanupScan:
        async with self._connection() as conn:
            withdraw = await conn.fetch(
                CLEANUP_WITHDRAW_LIST, agent, since, until, PREVIEW_WITHDRAW_LIMIT
            )
            withdraw_total = int(await conn.fetchval(CLEANUP_WITHDRAW_COUNT, agent, since, until))
            verdicts = await conn.fetch(CLEANUP_RATINGS_BY_VERDICT, agent, since, until)
            pins_total = int(
                await conn.fetchval(
                    CLEANUP_PINS_COUNT, agent, since, until, timeout=LONG_OP_TIMEOUT
                )
            )
            restorable = await conn.fetch(
                CLEANUP_RESTORABLE_LIST,
                agent,
                since,
                until,
                PREVIEW_RESTORE_LIMIT,
                timeout=LONG_OP_TIMEOUT,
            )
            restorable_total = int(
                await conn.fetchval(
                    CLEANUP_RESTORABLE_COUNT, agent, since, until, timeout=LONG_OP_TIMEOUT
                )
            )
        by_verdict = {r["verdict"]: int(r["n"]) for r in verdicts}
        return CleanupScan(
            since=since,
            until=until,
            withdraw=tuple(_row_to_entry(r) for r in withdraw),
            withdraw_total=withdraw_total,
            ratings_total=sum(by_verdict.values()),
            ratings_by_verdict=by_verdict,
            pins_total=pins_total,
            restorable=tuple(_row_to_entry(r) for r in restorable),
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
    ) -> CleanupOutcome:
        check_window(since, until)
        wanted = list(dict.fromkeys(restore_ids or ()))
        if any(not is_valid_uuid(w) for w in wanted):
            raise AgentCleanupRefused("restore_ids names memories that cannot be restored")
        wanted = [str(uuid.UUID(w)) for w in wanted]
        cleanup_id = str(uuid.uuid4())
        try:
            async with self._transaction() as conn:
                await conn.execute(MERGE_SETTINGS)
                # One cleanup or undo per agent at a time.
                await conn.execute(CLEANUP_AGENT_LOCK, agent)
                # Pin locks first, in fleet order (a merge takes the fleet rows,
                # then these), then the memory rows: a concurrent supersede or
                # withdraw of one waits for this commit, then sees its new state.
                fleets = sorted(
                    {
                        str(r["fleet_id"])
                        for r in await conn.fetch(CLEANUP_FLEETS, agent, since, until)
                    }
                    | {
                        str(r["fleet_id"])
                        for r in await conn.fetch(
                            CLEANUP_PIN_FLEETS, agent, since, until, timeout=LONG_OP_TIMEOUT
                        )
                    }
                )
                for fid in fleets:
                    await conn.execute(LOCK_FLEET_PINS, fid)
                locked = await conn.fetch(
                    CLEANUP_WITHDRAW_LOCK, agent, since, until, timeout=LONG_OP_TIMEOUT
                )
                withdraw_ids = [str(r["id"]) for r in locked]
                restorable = {
                    str(r["id"]): (str(r["first"]), str(r["head"]))
                    for r in await conn.fetch(
                        CLEANUP_RESTORABLE_LOCK, agent, since, until, timeout=LONG_OP_TIMEOUT
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
                    json.dumps(_pins_to_json(unpinned)),
                )
        except asyncpg.exceptions.LockNotAvailableError:
            raise AgentCleanupRefused(
                "the agent's memories are busy with writes or another cleanup; try again in a moment"
            ) from None
        except asyncpg.exceptions.DeadlockDetectedError:
            raise AgentCleanupRefused(
                "the cleanup deadlocked with a concurrent change; try it again"
            ) from None
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
        try:
            async with self._transaction() as conn:
                await conn.execute(MERGE_SETTINGS)
                row = await conn.fetchrow(CLEANUP_GET, cleanup_id)
                if row is None:
                    raise UnknownAgentCleanup(f"unknown agent cleanup: {cleanup_id}")
                await conn.execute(CLEANUP_AGENT_LOCK, row["agent"])
                # Re-read under the lock: a racing cleanup or undo has finished by now.
                mine = [
                    _row_to_cleanup(r) for r in await conn.fetch(CLEANUP_OF_AGENT, row["agent"])
                ]
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
                    home = {
                        str(r["id"])
                        for r in await conn.fetch(
                            CLEANUP_PINNABLE, [d.entry_id for d in mine_pins], fid
                        )
                    }
                    held = await conn.fetch(PINNED_ENTRY_IDS, fid)
                    pinned = {str(r["entry_id"]) for r in held}
                    fits = [d for d in mine_pins if d.entry_id in home and d.entry_id not in pinned]
                    skipped_pins.extend(d for d in mine_pins if d not in fits)
                    put, over = plan_pin_restore(fits, [r["pinned_by"] for r in held])
                    skipped_pins.extend(over)
                    for d in put:
                        await conn.execute(RESTORE_PIN, fid, d.entry_id, d.pinned_by, d.pinned_at)
                    restored_pins.extend(put)
                undone_at = await conn.fetchval(CLEANUP_MARK_UNDONE, cleanup_id)
        except asyncpg.exceptions.LockNotAvailableError:
            raise AgentCleanupRefused(
                "the agent's memories are busy with writes or another cleanup; try again in a moment"
            ) from None
        except asyncpg.exceptions.DeadlockDetectedError:
            raise AgentCleanupRefused(
                "the undo deadlocked with a concurrent change; try it again"
            ) from None
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

    async def list_agent_cleanups(self, limit: int) -> list[AgentCleanupRecord]:
        async with self._connection() as conn:
            rows = await conn.fetch(CLEANUP_LIST, limit)
        return [_row_to_cleanup(r) for r in rows]

    async def get_agent_cleanup(self, cleanup_id: str) -> AgentCleanupRecord | None:
        if not is_valid_uuid(cleanup_id):
            return None
        async with self._connection() as conn:
            row = await conn.fetchrow(CLEANUP_GET, cleanup_id)
        return _row_to_cleanup(row) if row is not None else None

    # -- agent registration / activation (ADR 0012) ----------------------------

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register an agent idempotently (SPEC §12.3).

        An existing record comes back unchanged (``owner_alias`` never
        overwritten, ADR 0039), including a racing duplicate or a
        case-only variant (ADR 0045, under a per-folded-name lock); a new
        one is ``pending`` (level 0, no fleet).
        """
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_AGENT, name)
            if row is None:
                async with conn.transaction():
                    await conn.execute(AGENT_NAME_LOCK, name)
                    row = await conn.fetchrow(GET_AGENT_FOLDED, name)
                    if row is None:
                        row = await conn.fetchrow(INSERT_AGENT, name, owner_alias)
                    if row is None:
                        # A concurrent registration won: return its record.
                        row = await conn.fetchrow(GET_AGENT, name)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between insert and read")
        return _row_to_agent(row)

    async def get_agent(self, name: str) -> Agent | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_AGENT, name)
        return _row_to_agent(row) if row is not None else None

    async def list_agents(self) -> list[Agent]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_AGENTS)
        return [_row_to_agent(r) for r in rows]

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Activate a pending or revoked agent: set trust level + home
        fleet, flip to ``active`` (ADRs 0012, 0028). ``UnknownAgent`` if
        unknown; ``InvalidAgentStatus`` if already ``active``.
        """
        async with self._transaction() as conn:
            await _refuse_merged(conn, home_fleet_id)
            # Under the row lock a concurrent retirement holds, so a retired
            # agent cannot slip back to active (ADR 0113).
            await conn.fetchrow(LOCK_AGENT_ROWS, [name])
            retired = await conn.fetchrow(GET_SUCCESSION, name)
            if retired is not None:
                raise AgentRetired(name, retired["successor"])
            row = await conn.fetchrow(ACTIVATE_AGENT, name, trust_level.value, home_fleet_id)
            if row is None:
                await self._raise_for_status(conn, name, "activate")
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between flip and read")
        return _row_to_agent(row)

    async def revoke_agent(self, name: str) -> Agent:
        """Flip a pending or active agent to ``revoked``.
        ``UnknownAgent`` if unknown; ``InvalidAgentStatus`` if already revoked.
        """
        async with self._connection() as conn:
            row = await conn.fetchrow(REVOKE_AGENT, name)
            if row is None:
                await self._raise_for_status(conn, name, "revoke")
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between flip and read")
        return _row_to_agent(row)

    @staticmethod
    async def _raise_for_status(conn: asyncpg.Connection, name: str, verb: str) -> None:
        """A guarded lifecycle UPDATE matched nothing: say why."""
        existing = await conn.fetchrow(GET_AGENT, name)
        if existing is None:
            raise UnknownAgent(f"unknown agent: {name}")
        raise InvalidAgentStatus(name, AgentStatus(existing["status"]), verb)

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Promote/demote an agent's trust level. ``UnknownAgent``
        if unknown. Demotion to level 0 is *dormant* (still active, no
        access) — distinct from revocation (ADR 0012).
        """
        async with self._connection() as conn:
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                raise UnknownAgent(f"unknown agent: {name}")
            row = await conn.fetchrow(SET_AGENT_LEVEL, name, level.value)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between check and update")
        return _row_to_agent(row)

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent to a new home fleet. The agent's
        earlier ``fleet``-scoped entries stay in the fleet they were written
        into (never re-parented). ``UnknownAgent`` if unknown.
        """
        async with self._transaction() as conn:
            await _refuse_merged(conn, fleet_id)
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                raise UnknownAgent(f"unknown agent: {name}")
            row = await conn.fetchrow(SET_AGENT_FLEET, name, fleet_id)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between check and update")
        return _row_to_agent(row)

    # -- successions (ADR 0113) ------------------------------------------------

    async def retire_agent(self, retired: str, successor: str, by: str) -> Succession:
        if retired == successor:
            raise InvalidInput("an agent cannot be its own successor")
        async with self._transaction() as conn:
            # Both rows, in name order: two opposite retirements cannot deadlock.
            status = {
                r["name"]: AgentStatus(r["status"])
                for r in await conn.fetch(LOCK_AGENT_ROWS, [retired, successor])
            }
            for name in (retired, successor):
                if name not in status:
                    raise UnknownAgent(f"unknown agent: {name}")
            if status[retired] is AgentStatus.PENDING:
                raise InvalidAgentStatus(retired, status[retired], "retire")
            if status[successor] is not AgentStatus.ACTIVE:
                raise InvalidAgentStatus(successor, status[successor], "make a successor of")
            standing = await conn.fetchrow(GET_SUCCESSION, retired)
            if standing is not None:
                raise SuccessionRefused(
                    f"agent {retired!r} is already retired in favour of {standing['successor']!r}"
                )
            if successor in await conn.fetchval(INHERITED_FROM, retired):
                raise SuccessionRefused(
                    f"{successor!r} already hands its memories down to {retired!r}, "
                    "directly or through others: that would be a cycle"
                )
            row = await conn.fetchrow(INSERT_SUCCESSION, retired, successor, by)
            await conn.execute(REVOKE_ACTIVE_AGENT, retired)
        if row is None:
            raise RuntimeError("succession row vanished between insert and read")
        return replace(_row_to_succession(row), was=status[retired])

    async def undo_succession(self, retired: str) -> Succession:
        async with self._connection() as conn:
            row = await conn.fetchrow(DELETE_SUCCESSION, retired)
        if row is None:
            raise UnknownSuccession(f"no succession retires {retired!r}")
        return _row_to_succession(row)

    async def get_succession(self, retired: str) -> Succession | None:
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_SUCCESSION, retired)
        return _row_to_succession(row) if row is not None else None

    async def list_successions(self) -> list[Succession]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_SUCCESSIONS)
        return [_row_to_succession(r) for r in rows]

    async def inherited_authors(self, name: str) -> tuple[str, ...]:
        async with self._connection() as conn:
            return tuple(await conn.fetchval(INHERITED_FROM, name))

    async def handed_down(self, authors: Sequence[str], home_fleet_id: str | None) -> HandedDown:
        names = list(authors)
        home = home_fleet_id if home_fleet_id and is_valid_uuid(home_fleet_id) else None
        async with self._connection() as conn:
            counts = await conn.fetchrow(HANDED_DOWN, names, home)
            pins = await conn.fetchval(HANDED_DOWN_PINS, names)
        assert counts is not None  # an aggregate always returns a row
        return HandedDown(
            authors=tuple(names),
            self_notes=int(counts["self_notes"]),
            fleet_memories=int(counts["fleet_memories"]),
            fleet_memories_elsewhere=int(counts["fleet_elsewhere"]),
            pins=int(pins),
        )

    # -- audit log (ADR 0027) -------------------------------------------------

    async def record_audit(self, event: AuditEvent) -> AuditRecord:
        """Append one audit row (insert-only). Failures propagate — an
        audit write is never swallowed (ADR 0027)."""
        async with self._connection() as conn:
            row = await conn.fetchrow(
                INSERT_AUDIT,
                event.actor_kind.value,
                event.actor,
                event.action.value,
                event.target,
                json.dumps(dict(event.detail)),
            )
        if row is None:
            raise RuntimeError("audit row vanished between insert and read")
        return _row_to_audit(row)

    async def list_audit(self, filters: AuditFilters, limit: int) -> list[AuditRecord]:
        """Matching audit rows, newest first, at most ``limit``."""
        conditions: list[str] = []
        params: list[Any] = []
        if filters.actor is not None:
            params.append(filters.actor)
            conditions.append(f"actor = ${len(params)}")
        if filters.action is not None:
            params.append(filters.action.value)
            conditions.append(f"action = ${len(params)}")
        if filters.since is not None:
            params.append(filters.since)
            conditions.append(f"occurred_at >= ${len(params)}")
        if filters.before is not None:
            # Row-value comparison against the cursor row (ADR 0028); an
            # unknown id yields a NULL row, which compares to nothing.
            params.append(filters.before)
            conditions.append(
                f"(occurred_at, id) < (SELECT occurred_at, id FROM audit_log "
                f"WHERE id = ${len(params)}::uuid)"
            )
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        sql = (
            f"SELECT {_AUDIT_COLUMNS} FROM audit_log {where} "
            f"ORDER BY occurred_at DESC, id DESC LIMIT ${len(params)}"
        )
        async with self._connection() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_audit(r) for r in rows]

    # -- orchestrator probes (ADR 0019) ---------------------------------------

    async def health_check(self) -> bool:
        """Deep probe: whether ``SELECT 1`` succeeds. Never
        raises; the endpoint's 503 is the signal.
        """
        try:
            async with self._connection() as conn:
                await conn.fetchval("SELECT 1")
        except Exception as exc:
            if self._database_reachable is not False:
                # The cause, once per outage: the probe response itself
                # never carries it (ADR 0037).
                logger.warning("database health check failing: %s: %s", type(exc).__name__, exc)
            self._database_reachable = False
            return False
        if self._database_reachable is False:
            logger.info("database health check passing again")
        self._database_reachable = True
        return True


# --- mappers --------------------------------------------------------------------


def _encode_sources(sources: tuple[Source, ...]) -> list[dict[str, str]]:
    """``sources`` is a jsonb column: a JSON list of {type, ref} objects."""
    return [{"type": source.type.value, "ref": source.ref} for source in sources]


def _encode_entities(entities: tuple[ExtractedEntity, ...]) -> list[dict[str, str]]:
    """``entities`` is a jsonb list of {name, kind}; the
    lower-cased names go separately on ``entity_names``."""
    return [{"name": e.name, "kind": e.kind.value} for e in entities]


def _row_to_feedback(row: asyncpg.Record) -> Feedback:
    return Feedback(
        entry_id=str(row["entry_id"]),
        user=row["user"],
        agent=row["agent"],
        verdict=Verdict(row["verdict"]),
        note=row["note"],
        updated_at=row["updated_at"],
    )


def _ages(rows: list[asyncpg.Record], kind: str) -> tuple[float, ...]:
    return tuple(float(r["age"]) for r in rows if r["kind"] == kind)


def _row_to_pin(row: asyncpg.Record) -> Pin:
    return Pin(
        fleet_id=row["fleet_id"],
        entry_id=str(row["entry_id"]),
        pinned_by=row["pinned_by"],
        pinned_at=row["pinned_at"],
    )


def _decode_sources(raw: Any) -> tuple[Source, ...]:
    """Decode the jsonb ``sources`` column back into ``Source`` objects.

    asyncpg decodes ``jsonb`` to native Python, so ``raw`` is a list (or
    None); the JSON-string branch is kept for raw-text drivers.
    """
    if not raw:
        return ()
    items = raw if isinstance(raw, list) else json.loads(raw)
    return tuple(Source(type=SourceType(item["type"]), ref=item["ref"]) for item in items)


def _decode_entities(raw: Any) -> tuple[ExtractedEntity, ...]:
    """Decode the jsonb ``entities`` column (native or JSON text)."""
    if not raw:
        return ()
    items = raw if isinstance(raw, list) else json.loads(raw)
    return tuple(
        ExtractedEntity(name=item["name"], kind=EntityKind(item["kind"])) for item in items
    )


def _row_to_entry(row: asyncpg.Record) -> Entry:
    payload = row["payload"]  # jsonb: native dict (or None)
    return Entry(
        id=str(row["id"]),
        kind=Kind(row["kind"]),
        summary=row["summary"],
        author=row["author"],
        agent=row["agent"],
        occurred_at=row["occurred_at"],
        created_at=row["created_at"],
        body=row["body"],
        payload=payload,
        sources=_decode_sources(row["sources"]),
        tags=tuple(row["tags"]) if row["tags"] else (),
        importance=row["importance"],
        importance_source=ImportanceSource(row["importance_source"]),
        scope=row["scope"],
        fleet_id=str(row["fleet_id"]) if row["fleet_id"] else None,
        embedding_model=row["embedding_model"],
        state=EntryState(row["state"]),
        superseded_by=str(row["superseded_by"]) if row["superseded_by"] else None,
        withdrawn_reason=row["withdrawn_reason"],
        entities=_decode_entities(row["entities"]),
        entities_model=row["entities_model"],
    )


def _row_to_fleet(row: asyncpg.Record) -> Fleet:
    """Map a ``fleets`` row to the domain ``Fleet``."""
    return Fleet(
        id=str(row["id"]),
        name=row["name"],
        created_at=row["created_at"],
        merged_into=str(row["merged_into"]) if row["merged_into"] is not None else None,
        merged_at=row["merged_at"],
    )


def _dropped_json(drops: Sequence[DroppedPin]) -> str:
    """The ``fleet_merges.dropped_pins`` jsonb for the pins a merge or move unpinned."""
    return json.dumps(
        [
            {
                "fleet_id": d.fleet_id,
                "entry_id": d.entry_id,
                "pinned_by": d.pinned_by,
                "pinned_at": d.pinned_at.isoformat(),
            }
            for d in drops
        ]
    )


def _row_to_merge(row: asyncpg.Record) -> FleetMergeRecord:
    """Map a ``fleet_merges`` row to the domain record."""
    raw = row["dropped_pins"]
    dropped = raw if isinstance(raw, list) else json.loads(raw)
    return FleetMergeRecord(
        id=str(row["id"]),
        source_fleet_id=str(row["source_fleet_id"]),
        target_fleet_id=str(row["target_fleet_id"]),
        merged_by=row["merged_by"],
        merged_at=row["merged_at"],
        moved_agents=tuple(row["moved_agents"]),
        dropped_pins=tuple(
            DroppedPin(
                d["fleet_id"], d["entry_id"], d["pinned_by"], datetime.fromisoformat(d["pinned_at"])
            )
            for d in dropped
        ),
        undone_at=row["undone_at"],
        kind=row["kind"],
    )


def _pins_to_json(pins: Sequence[DroppedPin]) -> list[dict[str, str]]:
    return [
        {
            "fleet_id": d.fleet_id,
            "entry_id": d.entry_id,
            "pinned_by": d.pinned_by,
            "pinned_at": d.pinned_at.isoformat(),
        }
        for d in pins
    ]


def _row_to_cleanup(row: asyncpg.Record) -> AgentCleanupRecord:
    """Map an ``agent_cleanups`` row to the domain record."""

    def loaded(raw: Any) -> list[dict[str, str]]:
        return raw if isinstance(raw, list) else json.loads(raw)

    return AgentCleanupRecord(
        id=str(row["id"]),
        agent=row["agent"],
        since=row["since"],
        until=row["until"],
        cleaned_by=row["cleaned_by"],
        cleaned_at=row["cleaned_at"],
        withdrawn_ids=tuple(str(i) for i in row["withdrawn_ids"]),
        restored=tuple(
            RestoredEntry(r["entry_id"], r["superseded_by"], r.get("head", ""))
            for r in loaded(row["restored"])
        ),
        ratings_set_aside=int(row["ratings_set_aside"]),
        unpinned_pins=tuple(
            DroppedPin(
                d["fleet_id"], d["entry_id"], d["pinned_by"], datetime.fromisoformat(d["pinned_at"])
            )
            for d in loaded(row["unpinned_pins"])
        ),
        revoked=bool(row["revoked"]),
        undone_at=row["undone_at"],
    )


def _row_to_succession(row: asyncpg.Record) -> Succession:
    return Succession(
        retired_agent=row["retired_agent"],
        successor=row["successor"],
        by=row["by"],
        at=row["at"],
    )


def _row_to_agent(row: asyncpg.Record) -> Agent:
    """Map an ``agents`` row to the domain ``Agent``."""
    return Agent(
        name=row["name"],
        status=AgentStatus(row["status"]),
        trust_level=TrustLevel(row["trust_level"]),
        home_fleet_id=str(row["home_fleet_id"]) if row["home_fleet_id"] else None,
        owner_alias=row["owner_alias"],
        created_at=row["created_at"],
        activated_at=row["activated_at"],
        # Only the agent list selects these; the other agent queries do not.
        last_used_at=row.get("last_used_at"),
        key_created_at=row.get("key_created_at"),
        key_locked_at=row.get("key_locked_at"),
    )


def _row_to_audit(row: asyncpg.Record) -> AuditRecord:
    """Map an ``audit_log`` row to the domain ``AuditRecord``.
    ``detail`` is jsonb: native dict or raw JSON text, per the codec."""
    detail = row["detail"]
    return AuditRecord(
        id=str(row["id"]),
        occurred_at=row["occurred_at"],
        actor_kind=ActorKind(row["actor_kind"]),
        actor=row["actor"],
        action=AuditAction(row["action"]),
        target=row["target"],
        detail=detail if isinstance(detail, dict) else json.loads(detail),
    )
