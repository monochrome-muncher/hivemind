"""The Postgres adapter for the ``Store`` port (asyncpg + pgvector).

Mirrors ``hivemind.memstore.MemoryStore`` behavior-for-behavior
(SPEC.md §4, §6): append-only entries with explicit supersession,
keyword (tsvector) and vector (cosine) search streams, filter
evaluation, and per-(entry, user, agent) feedback upserts.

Design notes:
- The connection pool is **lazily opened on first use** and closed via
  ``close()``: the API layer's synchronous builders hand a ready adapter
  to a running event loop without forcing an async pool at import time
  (SPEC.md §8.2, ADR 0007: one Postgres node per org, one pool per
  process at v1 scale).
- SQL lives in named constants; one method per ``Store`` op.
- Row<->``Entry`` mappers are pure functions at the bottom of the
  module (no I/O), keeping the async methods thin.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import asyncpg

from hivemind.domain.access import (
    Agent,
    AgentStatus,
    Fleet,
    InvalidAgentStatus,
    TrustLevel,
    Visibility,
)
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
    Source,
    SourceType,
    UsageCount,
)
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.pin import Pin
from hivemind.ports import SupersedeConflict
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
# 19 explicit columns/values; ``created_at`` falls back to the schema's
# ``now()`` default, so it is not in the value list.

# Every column an ``Entry`` is read from, except ``embedding``: no reader
# uses the stored vector (search ranks it in SQL), and fetching it cost
# ~4 KB on the wire plus a float-by-float decode per row — more than half
# of a 100-row list page's time. Entries read back carry ``embedding=None``.
_ENTRY_COLUMNS = """
    id, kind, summary, body, payload, sources, tags, occurred_at,
    created_at, author, agent, importance, importance_source, scope, fleet_id,
    embedding_model, state, superseded_by, withdrawn_reason,
    entities, entity_names, entities_model
"""

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

# The grouped usage counters (PERF-2): one scan instead of 10 + 3*agents +
# fleets COUNTs.
USAGE_COUNTS = """
SELECT scope, kind, importance_source, author, fleet_id,
       (state = 'active') AS active, count(*) AS n
  FROM entries
 GROUP BY scope, kind, importance_source, author, fleet_id, (state = 'active')
"""

# Pinned entries (ADR 0058). The per-fleet advisory lock serialises
# pinners of one fleet, so the count check and the insert are atomic.
LOCK_FLEET_PINS = "SELECT pg_advisory_xact_lock(hashtextextended('hivemind.pins:' || $1, 0))"
SELECT_PIN = "SELECT fleet_id, entry_id, pinned_by, pinned_at FROM pins WHERE fleet_id = $1 AND entry_id = $2"
COUNT_PINS = "SELECT count(*) FROM pins WHERE fleet_id = $1"
INSERT_PIN = """
INSERT INTO pins (fleet_id, entry_id, pinned_by) VALUES ($1, $2, $3)
RETURNING fleet_id, entry_id, pinned_by, pinned_at
"""
DELETE_PIN = "DELETE FROM pins WHERE fleet_id = $1 AND entry_id = $2"
LIST_PINS = """
SELECT fleet_id, entry_id, pinned_by, pinned_at FROM pins WHERE fleet_id = $1
 ORDER BY pinned_at DESC, entry_id
"""

# --- Fleet / agent access-control SQL (ADRs 0011-0012) ---------------------

SELECT_FLEET_BY_NAME = "SELECT 1 FROM fleets WHERE name = $1"
INSERT_FLEET = (
    "INSERT INTO fleets (name) VALUES ($1) "
    "ON CONFLICT (name) DO NOTHING RETURNING id, name, created_at"
)
GET_FLEET = "SELECT id, name, created_at FROM fleets WHERE id = $1"
LIST_FLEETS = "SELECT id, name, created_at FROM fleets ORDER BY created_at"

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
LIST_AGENTS = "SELECT * FROM agents ORDER BY name"
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
    "UPDATE entries SET state = 'withdrawn', withdrawn_reason = $2 "
    "WHERE id = $1 AND state = 'active' RETURNING " + _ENTRY_COLUMNS
)

# --- Keyword-stream query (ADR 0047) ------------------------------------------

MAX_KEYWORD_TERMS = 16
"""The most distinct query terms the keyword stream matches on (ADR 0049).

``ts_rank`` scores every matching row once per query term, and under the
any-term rule a single common term matches most of the pool. Without a
cap, a 2 000-character query of common words costs seconds of Postgres
CPU per search; with it, the worst case is a fixed multiple of a
one-term query. Terms past the cap are ignored by the keyword stream
(the vector stream still sees the whole query)."""

ANY_TERM_TSQUERY = (
    "(SELECT coalesce(string_agg(term, ' | ' ORDER BY first), '')::tsquery"
    " FROM (SELECT term, min(pos) AS first"
    " FROM unnest(string_to_array(plainto_tsquery('english', {param})::text, ' & '))"
    " WITH ORDINALITY AS t(term, pos)"
    f" GROUP BY term ORDER BY first LIMIT {MAX_KEYWORD_TERMS}) AS terms)"
)
"""The keyword stream's tsquery: any query term matches (ADR 0047), on
at most ``MAX_KEYWORD_TERMS`` distinct terms (ADR 0049).

``plainto_tsquery`` still does all the parsing, so the caller's text is
never read as tsquery syntax (no operator injection). Its output joins
quoted lexemes with ``' & '``. Splitting on that separator gives one
quoted lexeme per element, because the ``english`` parser never produces
a lexeme containing a space. The first ``MAX_KEYWORD_TERMS`` distinct
lexemes, in query order, are re-joined with ``' | '``, which turns
"every term" into "any term". A query of only stop words gives an empty
tsquery, which matches nothing, as before. The scalar subquery runs once
per statement, not once per row.
"""

# --- Vector-search session settings (ADR 0025) --------------------------------

VECTOR_SEARCH_SETTINGS = """
SET LOCAL hnsw.iterative_scan = strict_order;
SET LOCAL hnsw.ef_search = 40;
"""
"""The query-time GUCs the HNSW index (migration ``0004``) is read under.

``SET LOCAL``, inside the same transaction as the search, rather than a
pool ``init`` callback: the org runs a **transaction-mode PgBouncer**
(see ``store/pool.py``), where one pooled client connection is mapped to
whichever server connection is free *per transaction*, and
``server_reset_query`` (``DISCARD ALL``) wipes session state between
them. A session-level ``SET`` at connection-open time would therefore
land on an arbitrary server backend and be discarded before the search
ever runs — silently, leaving the defaults in force. A ``SET LOCAL``
travels with its transaction, so it reaches the backend that executes
the query, and it unsets at commit, so it never leaks into the other
queries sharing the pooled connection.

``hnsw.iterative_scan = strict_order`` (pgvector >= 0.8): without it,
HNSW fetches ``ef_search`` candidates and applies the ``WHERE`` clause
*afterwards*. Every real query here is filtered — at minimum
``state = 'active'``, plus the ADR 0011 visibility matrix — so a
narrow-visibility reader could get far fewer than ``candidate_top_k``
rows out of the vector stream, starving RRF's second list. Iterative
scan keeps resuming the search until the limit is satisfied (bounded by
``hnsw.max_scan_tuples``). ``strict_order`` and not ``relaxed_order``
because RRF fuses on **ranks, not scores** (``retrieval/rrf.py``):
relaxed_order returns results slightly out of distance order, which
perturbs exactly the quantity fusion consumes.

``hnsw.ef_search = 40`` is pgvector's own default, set explicitly so the
value the vector stream runs under is stated rather than inherited.
There is no corpus to tune it against yet (ADR 0025).
"""

UPSERT_FEEDBACK = """
INSERT INTO feedbacks (entry_id, "user", agent, verdict, note, updated_at)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (entry_id, "user", agent) DO UPDATE
SET verdict = EXCLUDED.verdict,
    note = EXCLUDED.note,
    updated_at = EXCLUDED.updated_at
"""

COUNT_FEEDBACK = """
SELECT
    COUNT(*) FILTER (WHERE verdict = 'helpful') AS helpful,
    COUNT(*) FILTER (WHERE verdict = 'stale') AS stale,
    COUNT(*) FILTER (WHERE verdict = 'wrong') AS wrong
FROM feedbacks
WHERE entry_id = $1
"""

LIST_FEEDBACK = """
SELECT entry_id, "user", agent, verdict, note, updated_at
FROM feedbacks
WHERE entry_id = $1
ORDER BY updated_at DESC, "user", agent
LIMIT $2
"""

BATCH_FEEDBACK = """
SELECT
    entry_id,
    COUNT(*) FILTER (WHERE verdict = 'helpful') AS helpful,
    COUNT(*) FILTER (WHERE verdict = 'stale') AS stale,
    COUNT(*) FILTER (WHERE verdict = 'wrong') AS wrong
FROM feedbacks
WHERE entry_id = ANY($1)
GROUP BY entry_id
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
    """Translate an ``EntryFilters`` (and an optional ``Visibility``, ADR
    0011) into WHERE-fragments + parameters.

    Mirrors ``EntryFilters.matches`` exactly (SPEC.md §5.3): by default
    only active entries are visible; ``include_inactive`` lifts the state
    restriction. Tags use AND-semantics (``tags @> $n``). When
    ``visibility`` is supplied, an extra clause restricts the result to
    entries visible to that reader (the trust-level matrix, ADR 0011);
    ``None`` keeps the v1 flat-pool behavior.
    """
    clauses: list[str] = []
    params: list[Any] = []

    def add(fragment: str, value: Any) -> None:
        params.append(value)
        clauses.append(fragment.replace("?", f"${len(params)}"))

    if not filters.include_inactive:
        clauses.append("state = 'active'")
    if filters.kind is not None:
        add("kind = ?", filters.kind.value)
    if filters.importance_source is not None:
        add("importance_source = ?", filters.importance_source.value)
    if filters.tags:
        add("tags @> ?", list(filters.tags))
    if filters.entities:
        # AND-semantics over lower-cased names (ADR 0016, SPEC §13):
        # the filter names are case-insensitive; ``entity_names`` holds
        # the lower-cased names, symmetric with the ``tags`` pattern.
        add("entity_names @> ?", [name.lower() for name in filters.entities])
    if filters.scope is not None:
        add("scope = ?", filters.scope)
    if filters.fleet_id is not None:
        add("fleet_id = ?", filters.fleet_id)
    if filters.author is not None:
        add("author = ?", filters.author)
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
            " AND f.verdict IN ('stale', 'wrong'))"
        )

    vis_clause = _visibility_clause(visibility, params)
    if vis_clause is not None:
        clauses.append(vis_clause)

    return clauses, params


def _visibility_clause(visibility: Visibility | None, params: list[Any]) -> str | None:
    """The SQL fragment restricting results to entries visible to
    ``visibility`` (ADR 0011). Appends its parameters to ``params`` and
    returns the clause (or ``None`` for no restriction).

    Mirrors ``domain.access.entry_is_visible``:
      * ``None`` / admin  -> no clause (see everything / v1 flat pool).
      * level 0 (untrusted) -> ``FALSE`` (see nothing).
      * level 1/2 (lurker/contributor) -> ``org`` OR own ``self`` OR
        (own ``fleet`` within the home fleet).
      * level 3 (privileged) -> ``org`` OR own ``self`` OR any ``fleet``
        (read-broad, write-local).
    """
    if visibility is None or visibility.is_admin:
        return None
    if visibility.level == TrustLevel.UNTRUSTED:
        return "FALSE"

    def _p(value: Any) -> str:
        params.append(value)
        return f"${len(params)}"

    name_p = _p(visibility.name)
    if visibility.level == TrustLevel.PRIVILEGED:
        # L3: read-broad — every fleet's ``fleet`` entries are visible.
        return f"(scope = 'org' OR (scope = 'self' AND author = {name_p}) OR scope = 'fleet')"
    # L1/L2: own + home fleet (+ legacy org). Fleet entries are visible
    # only if they are the reader's own or in the reader's home fleet.
    home_p = _p(visibility.home_fleet_id)
    return (
        "(scope = 'org' "
        f"OR (scope = 'self' AND author = {name_p}) "
        f"OR (scope = 'fleet' AND (author = {name_p} OR fleet_id = {home_p})))"
    )


def _is_valid_uuid(value: str) -> bool:
    """Whether ``value`` parses as a UUID (the ``id`` column is a pg ``uuid``)."""
    try:
        uuid.UUID(value)
    except ValueError, TypeError:
        return False
    return True


def _canonical_uuid(value: str) -> str | None:
    """``value`` in Postgres's canonical ``uuid`` text form, or ``None``.

    Both ``uuid.UUID`` and Postgres accept non-canonical spellings
    (upper case, no hyphens, braces); comparisons against ids read back
    from the database must use the canonical form."""
    try:
        return str(uuid.UUID(value))
    except ValueError, TypeError:
        return None


def _valid_uuids(ids: list[str] | tuple[str, ...]) -> list[str]:
    """Keep only the IDs that parse as UUIDs (others simply don't exist)."""
    return [i for i in ids if _is_valid_uuid(i)]


class PgStore:
    """A ``Store`` backed by PostgreSQL + pgvector (ADR 0007).

    Construct with a DSN (the pool opens lazily on first use) and
    ``close()`` on process shutdown. ``pool_min_size`` / ``pool_max_size``
    (default 1 / 10, matching ``make_pool``'s own defaults) are the
    operator knobs for per-pod concurrency (``Settings.pool_min_size`` /
    ``pool_max_size``).
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
        # Serialises lazy creation: without it, every call arriving while the
        # first pool is still being built opened its own and orphaned it.
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
        to the ``superseded`` state (SPEC.md §4.1).

        The insert and the supersession flip run in a single transaction
        (m4): a crash between them must not leave a new entry whose
        supersession targets were never flipped. ``embedding_model``
        (SPEC.md §7) records which model produced ``embedding``.
        ``entities`` / ``entities_model`` (ADR 0016, SPEC §13) record
        the machine-extracted facets + the extractor that produced them.
        """
        entry_id = str(uuid.uuid4())
        occurred_at = draft.resolved_occurred_at()

        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
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
                json.dumps(_encode_entities(entities)),  # jsonb (ADR 0016)
                [e.name.lower() for e in entities],  # lower-cased names: the filter column
                entities_model,  # ADR 0016: extractor model (provenance)
            )
            if draft.supersedes:
                # ADR 0034, atomic: the guarded UPDATE row-locks the
                # targets; any target it did not flip (unknown,
                # non-UUID, or no longer active) aborts the whole
                # transaction, insert included.
                wanted = list(dict.fromkeys(draft.supersedes))
                flipped = {
                    str(r["id"])
                    for r in await conn.fetch(FLIP_SUPERSEDED, entry_id, _valid_uuids(wanted))
                }
                denied = [t for t in wanted if _canonical_uuid(t) not in flipped]
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
        if not _is_valid_uuid(entry_id):
            return None
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            row = await conn.fetchrow(SELECT_ENTRY, entry_id)
        return _row_to_entry(row) if row is not None else None

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        ids = _valid_uuids(entry_ids)
        if not ids:
            return {}
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(SELECT_ENTRIES, ids)
        return {str(row["id"]): _row_to_entry(row) for row in rows}

    async def list_predecessors(self, entry_ids: list[str]) -> list[Entry]:
        ids = _valid_uuids(entry_ids)
        if not ids:
            return []
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(SELECT_PREDECESSORS, ids)
        return [_row_to_entry(row) for row in rows]

    async def pin_entry(
        self, fleet_id: str, entry_id: str, pinned_by: str, limit: int
    ) -> Pin | None:
        if not _is_valid_uuid(entry_id):
            return None
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            await conn.execute(LOCK_FLEET_PINS, fleet_id)
            row = await conn.fetchrow(SELECT_PIN, fleet_id, entry_id)
            if row is None:
                if await conn.fetchval(COUNT_PINS, fleet_id) >= limit:
                    return None
                row = await conn.fetchrow(INSERT_PIN, fleet_id, entry_id, pinned_by)
        return _row_to_pin(row)

    async def unpin_entry(self, fleet_id: str, entry_id: str) -> bool:
        if not _is_valid_uuid(entry_id):
            return False
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            status = await conn.execute(DELETE_PIN, fleet_id, entry_id)
        return bool(status.endswith(" 1"))

    async def list_pins(self, fleet_id: str) -> list[Pin]:
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(LIST_PINS, fleet_id)
        return [_row_to_pin(row) for row in rows]

    async def usage_counts(self) -> list[UsageCount]:
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
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
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(sql, *params, limit, offset)
        return [_row_to_entry(row) for row in rows]

    async def count_entries(
        self, filters: EntryFilters, *, visibility: Visibility | None = None
    ) -> int:
        """Count entries matching ``filters`` (the minimal usage-counters
        surface, ROADMAP §3.3) — a cheap ``COUNT`` in Postgres, not a
        full fetch. ``visibility`` behaves like ``list_entries``
        (ADR 0011); ``None`` keeps the v1 flat-pool count."""
        clauses, params = _filter_conditions(filters, visibility)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = "SELECT count(*) AS n FROM entries WHERE " + where
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            row = await conn.fetchrow(sql, *params)
        return int(row["n"]) if row is not None else 0

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        """Flip an entry to ``withdrawn`` (SPEC.md §4.1).

        Raises ``KeyError`` if the entry does not exist and
        ``ValueError`` if it is not active (the port contract).
        ``by_user`` is the API-layer audit of who withdrew it; v1
        persists the reason only (SPEC.md §4.1 lists ``withdrawn_reason``,
        not a ``withdrawn_by`` column).
        """
        if not _is_valid_uuid(entry_id):
            raise KeyError(f"unknown entry: {entry_id}")
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            existing = await conn.fetchrow(LOOKUP_STATE, entry_id)
            if existing is None:
                raise KeyError(f"unknown entry: {entry_id}")
            if existing["state"] != "active":
                raise ValueError(f"entry {entry_id} is {existing['state']}, not active")
            row = await conn.fetchrow(WITHDRAW, entry_id, reason)
            if row is None:
                # Lost the race on the guarded UPDATE: a concurrent
                # withdraw/supersede flipped the state between the
                # pre-check and the update. Disambiguate by re-reading.
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
        """Ranked entry IDs by ``ts_rank`` over the maintained tsvector
        (SPEC.md §6.2 keyword stream). An entry matches when it contains
        **any** query term (ADR 0047), the same rule as ``MemoryStore``;
        ``ts_rank`` puts entries matching more terms first. Only the first
        ``MAX_KEYWORD_TERMS`` distinct terms count (ADR 0049). See
        ``ANY_TERM_TSQUERY`` for how the query is built without operator
        injection. When ``visibility`` is supplied, only visible entries
        are candidates (ADR 0011)."""
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
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(sql, *params, limit)
        return [str(r["id"]) for r in rows]

    async def search_vector(
        self,
        embedding: list[float],
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
    ) -> list[str]:
        """Ranked entry IDs by cosine distance to ``embedding``
        (SPEC.md §6.2 vector stream; the pgvector ``<=>`` operator).
        When ``visibility`` is supplied, only visible entries are
        candidates (ADR 0011).

        Served by the HNSW index ``entries_embedding_hnsw_idx``
        (migration ``0004``), so this is **approximate** nearest-neighbour
        search: the returned ids are not guaranteed to be the true top-``limit``
        by cosine distance (ADR 0025). ``VECTOR_SEARCH_SETTINGS`` is applied
        with ``SET LOCAL`` in the same transaction — see its docstring for
        why the settings live here and not on the pool's connection ``init``.
        """
        clauses, params = _filter_conditions(filters, visibility)
        clauses.append("embedding IS NOT NULL")
        vec_idx = len(params) + 1
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = (
            "SELECT id FROM entries WHERE "
            + where
            + f" ORDER BY embedding <=> ${vec_idx}"
            + f" LIMIT ${vec_idx + 1}"
        )
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            await conn.execute(VECTOR_SEARCH_SETTINGS)
            rows = await conn.fetch(sql, *params, embedding, limit)
        return [str(r["id"]) for r in rows]

    async def similar_entries(
        self,
        embedding: list[float],
        limit: int,
        *,
        exclude_id: str,
        visibility: Visibility | None = None,
    ) -> list[tuple[str, float]]:
        """The active entries nearest to ``embedding`` with their cosine
        similarity (ADR 0052). The same index, filters and ``SET LOCAL``
        settings as ``search_vector``, so it is approximate too (ADR 0025)."""
        clauses, params = _filter_conditions(EntryFilters(), visibility)
        clauses.append("embedding IS NOT NULL")
        if _is_valid_uuid(exclude_id):
            params.append(exclude_id)
            clauses.append(f"id <> ${len(params)}")
        vec_idx = len(params) + 1
        sql = (
            f"SELECT id, 1 - (embedding <=> ${vec_idx}) AS similarity FROM entries WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY embedding <=> ${vec_idx}"
            + f" LIMIT ${vec_idx + 1}"
        )
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn, conn.transaction():
            await conn.execute(VECTOR_SEARCH_SETTINGS)
            rows = await conn.fetch(sql, *params, embedding, limit)
        return [(str(r["id"]), float(r["similarity"])) for r in rows]

    # -- feedback -----------------------------------------------------------------

    async def record_feedback(self, feedback: Feedback) -> None:
        """Upsert a feedback verdict (one row per entry+user+agent)."""
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            await conn.execute(
                UPSERT_FEEDBACK,
                feedback.entry_id,
                feedback.user,
                feedback.agent,
                feedback.verdict.value,
                feedback.note,
                feedback.updated_at or datetime.now(UTC),
            )

    async def feedback_counts(self, entry_id: str) -> tuple[int, int, int]:
        if not _is_valid_uuid(entry_id):
            return (0, 0, 0)
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            row = await conn.fetchrow(COUNT_FEEDBACK, entry_id)
        return (row["helpful"], row["stale"], row["wrong"]) if row else (0, 0, 0)

    async def quality_counts(self, entry_ids: list[str]) -> dict[str, tuple[int, int, int]]:
        """Batched feedback counts for a set of entries.

        Mirrors the reference store: every requested ID is present, with
        zero counts for entries that have no feedback rows yet.
        """
        ids = _valid_uuids(entry_ids)
        if not ids:
            return {}
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(BATCH_FEEDBACK, ids)
        counts = dict.fromkeys(ids, (0, 0, 0))
        for r in rows:
            counts[str(r["entry_id"])] = (r["helpful"], r["stale"], r["wrong"])
        return counts

    async def list_feedback(self, entry_id: str, limit: int) -> list[Feedback]:
        """An entry's newest feedback rows (ADR 0051). The primary key
        ``(entry_id, "user", agent)`` narrows the scan to one entry's rows."""
        if not _is_valid_uuid(entry_id):
            return []
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(LIST_FEEDBACK, entry_id, limit)
        return [
            Feedback(
                entry_id=str(r["entry_id"]),
                user=r["user"],
                agent=r["agent"],
                verdict=Verdict(r["verdict"]),
                note=r["note"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    # -- fleets (ADR 0011) ------------------------------------------------------

    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet (ADR 0011). ``ValueError`` if the name
        already exists — including the concurrent case: the insert is
        conflict-guarded, so a racing duplicate is a typed error, not a
        raw ``UniqueViolation``."""
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            dup = await conn.fetchrow(SELECT_FLEET_BY_NAME, name)
            if dup is not None:
                raise ValueError(f"fleet already exists: {name!r}")
            row = await conn.fetchrow(INSERT_FLEET, name)
            if row is None:
                # A concurrent create won the race between the check and
                # the conflict-guarded insert.
                raise ValueError(f"fleet already exists: {name!r}")
        return _row_to_fleet(row)

    async def list_fleets(self) -> list[Fleet]:
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(LIST_FLEETS)
        return [_row_to_fleet(r) for r in rows]

    async def get_fleet(self, fleet_id: str) -> Fleet | None:
        if not _is_valid_uuid(fleet_id):
            return None
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            row = await conn.fetchrow(GET_FLEET, fleet_id)
        return _row_to_fleet(row) if row is not None else None

    # -- agent registration / activation (ADR 0012) ----------------------------

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register (or re-register) an agent (ADR 0012). Idempotent: an
        existing record is returned unchanged (its ``owner_alias`` is never
        overwritten, ADR 0039); a new record is ``pending`` (level 0, no fleet). A
        racing concurrent registration of the same name returns the same
        pending record (the insert is conflict-guarded — no raw
        ``UniqueViolation``, SPEC §12.3 idempotent no-op). A name that only
        differs in case from an existing agent's is not inserted: that
        agent comes back instead (ADR 0045), under a per-folded-name lock.
        """
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            row = await conn.fetchrow(GET_AGENT, name)
            if row is None:
                async with conn.transaction():
                    await conn.execute(AGENT_NAME_LOCK, name)
                    row = await conn.fetchrow(GET_AGENT_FOLDED, name)
                    if row is None:
                        row = await conn.fetchrow(INSERT_AGENT, name, owner_alias)
                    if row is None:
                        # A concurrent registration of this exact name won
                        # the conflict-guarded insert: return its record.
                        row = await conn.fetchrow(GET_AGENT, name)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between insert and read")
        return _row_to_agent(row)

    async def get_agent(self, name: str) -> Agent | None:
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            row = await conn.fetchrow(GET_AGENT, name)
        return _row_to_agent(row) if row is not None else None

    async def list_agents(self) -> list[Agent]:
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(LIST_AGENTS)
        return [_row_to_agent(r) for r in rows]

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Activate a pending or revoked agent: set trust level + home
        fleet, flip to ``active`` (ADRs 0012, 0028). ``KeyError`` if
        unknown; ``InvalidAgentStatus`` if already ``active``.
        """
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            row = await conn.fetchrow(ACTIVATE_AGENT, name, trust_level.value, home_fleet_id)
            if row is None:
                await self._raise_for_status(conn, name, "activate")
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between flip and read")
        return _row_to_agent(row)

    async def revoke_agent(self, name: str) -> Agent:
        """Flip a pending or active agent to ``revoked`` (ADR 0028).
        ``KeyError`` if unknown; ``InvalidAgentStatus`` if already revoked.
        """
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
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
            raise KeyError(f"unknown agent: {name}")
        raise InvalidAgentStatus(name, AgentStatus(existing["status"]), verb)

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Promote/demote an agent's trust level (ADR 0011). ``KeyError``
        if unknown. Demotion to level 0 is *dormant* (still active, no
        access) — distinct from revocation (ADR 0012).
        """
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                raise KeyError(f"unknown agent: {name}")
            row = await conn.fetchrow(SET_AGENT_LEVEL, name, level.value)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between check and update")
        return _row_to_agent(row)

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent to a new home fleet (ADR 0011). The agent's
        earlier ``fleet``-scoped entries stay in the fleet they were written
        into (never re-parented). ``KeyError`` if unknown.
        """
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                raise KeyError(f"unknown agent: {name}")
            row = await conn.fetchrow(SET_AGENT_FLEET, name, fleet_id)
        if row is None:
            raise RuntimeError(f"agent {name!r} vanished between check and update")
        return _row_to_agent(row)

    # -- audit log (ADR 0027) -------------------------------------------------

    async def record_audit(self, event: AuditEvent) -> AuditRecord:
        """Append one audit row (insert-only). Failures propagate — an
        audit write is never swallowed (ADR 0027)."""
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
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
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_audit(r) for r in rows]

    # -- orchestrator probes (ADR 0019) ---------------------------------------

    async def health_check(self) -> bool:
        """Deep liveness probe (ADR 0019): True when the pool is up and
        answering a ``SELECT 1``, False when it is unreachable or has
        dropped the connection. The probe must never raise — the 503
        from the probe endpoint is the orchestrator-facing signal.
        """
        try:
            pool = await self._ensure_pool()
            async with pool.acquire(timeout=self._acquire_timeout) as conn:
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
    """``entities`` is a jsonb column: a JSON list of {name, kind} facets
    (ADR 0016). The lower-cased names are stored separately on
    ``entity_names`` (the filter column, symmetric with ``tags``)."""
    return [{"name": e.name, "kind": e.kind.value} for e in entities]


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
    """Decode the jsonb ``entities`` column back into domain facets.

    asyncpg may deliver ``jsonb`` natively (a list of {name, kind}
    dicts) or as raw JSON text, depending on the codec — mirror the
    dual-decode of ``_decode_sources``.
    """
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
        entities=_decode_entities(row["entities"]),  # ADR 0016
        entities_model=row["entities_model"],
    )


def _row_to_fleet(row: asyncpg.Record) -> Fleet:
    """Map a ``fleets`` row to the domain ``Fleet`` (ADR 0011)."""
    return Fleet(id=str(row["id"]), name=row["name"], created_at=row["created_at"])


def _row_to_agent(row: asyncpg.Record) -> Agent:
    """Map an ``agents`` row to the domain ``Agent`` (ADR 0012)."""
    return Agent(
        name=row["name"],
        status=AgentStatus(row["status"]),
        trust_level=TrustLevel(row["trust_level"]),
        home_fleet_id=str(row["home_fleet_id"]) if row["home_fleet_id"] else None,
        owner_alias=row["owner_alias"],
        created_at=row["created_at"],
        activated_at=row["activated_at"],
    )


def _row_to_audit(row: asyncpg.Record) -> AuditRecord:
    """Map an ``audit_log`` row to the domain ``AuditRecord`` (ADR 0027).
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
