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
import re
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
    SearchCount,
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
INSERT INTO search_counts (fleet_id, searches, empty) VALUES ($1, 1, $2)
ON CONFLICT (fleet_id) DO UPDATE
   SET searches = search_counts.searches + 1,
       empty = search_counts.empty + EXCLUDED.empty
"""
SEARCH_COUNTS = "SELECT fleet_id, searches, empty FROM search_counts"

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

# The newest $2 rows of each entry in $1 (ADR 0059), in LIST_FEEDBACK order.
LIST_FEEDBACK_MANY = """
SELECT entry_id, "user", agent, verdict, note, updated_at FROM (
    SELECT f.*, row_number() OVER (
               PARTITION BY entry_id ORDER BY updated_at DESC, "user", agent) AS rn
      FROM feedbacks f
     WHERE entry_id = ANY($1::uuid[])
) AS ranked
WHERE rn <= $2
ORDER BY entry_id, updated_at DESC, "user", agent
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

    if not filters.include_inactive:
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
    """The visibility clause for ``visibility`` (ADR 0011), or ``None``
    for no restriction; appends its parameters to ``params``.

    Mirrors ``domain.access.entry_is_visible``.
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
        # L3 reads every fleet's entries.
        return f"(scope = 'org' OR (scope = 'self' AND author = {name_p}) OR scope = 'fleet')"
    # L1/L2: fleet entries only if own or in the home fleet.
    home_p = _p(visibility.home_fleet_id)
    return (
        "(scope = 'org' "
        f"OR (scope = 'self' AND author = {name_p}) "
        f"OR (scope = 'fleet' AND (author = {name_p} OR fleet_id = {home_p})))"
    )


# What asyncpg's ``uuid`` codec accepts: 32 hex digits, hyphens anywhere,
# at most 36 characters. ``uuid.UUID`` is laxer (braces, ``urn:uuid:``,
# any number of hyphens), and asyncpg answers those with a ``DataError``.
_UUID_TEXT_RE = re.compile(r"[0-9A-Fa-f-]{32,36}")


def _is_valid_uuid(value: str) -> bool:
    """Whether ``value`` is a UUID spelling the ``uuid`` columns accept."""
    if not isinstance(value, str) or not _UUID_TEXT_RE.fullmatch(value):
        return False
    try:
        uuid.UUID(value)
    except ValueError:
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

    async def list_predecessors(self, entry_ids: list[str]) -> list[Entry]:
        ids = _valid_uuids(entry_ids)
        if not ids:
            return []
        async with self._connection() as conn:
            rows = await conn.fetch(SELECT_PREDECESSORS, ids)
        return [_row_to_entry(row) for row in rows]

    async def entry_links(self, entry_id: str, limit: int) -> tuple[list[str], list[str]]:
        if not _is_valid_uuid(entry_id):
            return [], []
        async with self._connection() as conn:
            outgoing = await conn.fetch(SELECT_LINKS_OUT, entry_id)
            incoming = await conn.fetch(SELECT_LINKS_IN, entry_id, limit)
        return [str(r["to_id"]) for r in outgoing], [str(r["from_id"]) for r in incoming]

    async def entry_links_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, tuple[list[str], list[str]]]:
        canonical = {eid: _canonical_uuid(eid) for eid in entry_ids}
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

    async def pin_entry(
        self, fleet_id: str, entry_id: str, pinned_by: str, limit: int
    ) -> Pin | None:
        if not _is_valid_uuid(entry_id):
            return None
        async with self._transaction() as conn:
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

    async def record_search(self, fleet_id: str | None, *, empty: bool) -> None:
        async with self._connection() as conn:
            await conn.execute(RECORD_SEARCH, fleet_id or "", int(empty))

    async def search_counts(self) -> list[SearchCount]:
        async with self._connection() as conn:
            rows = await conn.fetch(SEARCH_COUNTS)
        return [
            SearchCount(
                fleet_id=row["fleet_id"] or None,
                searches=int(row["searches"]),
                empty=int(row["empty"]),
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
        if not _is_valid_uuid(entry_id):
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
        Any query term matches (ADR 0047); see ``ANY_TERM_TSQUERY``."""
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
        async with self._transaction() as conn:
            await conn.execute(VECTOR_SEARCH_SETTINGS)
            rows = await conn.fetch(sql, *params, embedding, limit)
        return [(str(r["id"]), float(r["similarity"])) for r in rows]

    # -- feedback -----------------------------------------------------------------

    async def record_feedback(self, feedback: Feedback) -> None:
        """Upsert a feedback verdict (one row per entry+user+agent)."""
        async with self._connection() as conn:
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

    async def list_feedback(self, entry_id: str, limit: int) -> list[Feedback]:
        """An entry's newest feedback rows (ADR 0051). The primary key
        ``(entry_id, "user", agent)`` narrows the scan to one entry's rows."""
        if not _is_valid_uuid(entry_id):
            return []
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_FEEDBACK, entry_id, limit)
        return [_row_to_feedback(r) for r in rows]

    async def list_feedback_many(
        self, entry_ids: list[str], limit: int
    ) -> dict[str, list[Feedback]]:
        canonical = {eid: _canonical_uuid(eid) for eid in entry_ids}
        ids = list(dict.fromkeys(c for c in canonical.values() if c is not None))
        rows_by_id: dict[str, list[Feedback]] = {c: [] for c in ids}
        if ids:
            async with self._connection() as conn:
                rows = await conn.fetch(LIST_FEEDBACK_MANY, ids, limit)
            for r in rows:
                rows_by_id[str(r["entry_id"])].append(_row_to_feedback(r))
        return {eid: rows_by_id[c] if c is not None else [] for eid, c in canonical.items()}

    # -- fleets (ADR 0011) ------------------------------------------------------

    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet (ADR 0011). ``ValueError`` if the name
        exists, including a racing duplicate (conflict-guarded insert)."""
        async with self._connection() as conn:
            dup = await conn.fetchrow(SELECT_FLEET_BY_NAME, name)
            if dup is not None:
                raise ValueError(f"fleet already exists: {name!r}")
            row = await conn.fetchrow(INSERT_FLEET, name)
            if row is None:
                # A concurrent create won the race.
                raise ValueError(f"fleet already exists: {name!r}")
        return _row_to_fleet(row)

    async def list_fleets(self) -> list[Fleet]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_FLEETS)
        return [_row_to_fleet(r) for r in rows]

    async def get_fleet(self, fleet_id: str) -> Fleet | None:
        if not _is_valid_uuid(fleet_id):
            return None
        async with self._connection() as conn:
            row = await conn.fetchrow(GET_FLEET, fleet_id)
        return _row_to_fleet(row) if row is not None else None

    # -- agent registration / activation (ADR 0012) ----------------------------

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register an agent idempotently (ADR 0012, SPEC §12.3).

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
        fleet, flip to ``active`` (ADRs 0012, 0028). ``KeyError`` if
        unknown; ``InvalidAgentStatus`` if already ``active``.
        """
        async with self._connection() as conn:
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
            raise KeyError(f"unknown agent: {name}")
        raise InvalidAgentStatus(name, AgentStatus(existing["status"]), verb)

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Promote/demote an agent's trust level (ADR 0011). ``KeyError``
        if unknown. Demotion to level 0 is *dormant* (still active, no
        access) — distinct from revocation (ADR 0012).
        """
        async with self._connection() as conn:
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
        async with self._connection() as conn:
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
        """Deep probe (ADR 0019): whether ``SELECT 1`` succeeds. Never
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
    """``entities`` is a jsonb list of {name, kind} (ADR 0016); the
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
