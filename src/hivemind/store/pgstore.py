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

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg

from hivemind.domain.access import (
    Agent,
    AgentStatus,
    Fleet,
    TrustLevel,
    Visibility,
)
from hivemind.domain.entry import (
    Entry,
    EntryDraft,
    EntryFilters,
    EntryState,
    Kind,
    Source,
    SourceType,
)
from hivemind.domain.feedback import Feedback
from hivemind.store.pool import make_pool

# --- SQL ----------------------------------------------------------------------

INSERT_ENTRY = """
INSERT INTO entries (
    id, kind, summary, body, payload, sources, tags,
    occurred_at, author, agent, importance, scope, fleet_id,
    embedding, embedding_model
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15
)
"""
# 15 explicit columns/values; ``created_at`` falls back to the schema's
# ``now()`` default, so it is not in the value list.

_ENTRY_COLUMNS = """
    id, kind, summary, body, payload, sources, tags, occurred_at,
    created_at, author, agent, importance, scope, fleet_id, embedding,
    embedding_model, state, superseded_by, withdrawn_reason
"""

SELECT_ENTRY = "SELECT " + _ENTRY_COLUMNS + " FROM entries WHERE id = $1"

SELECT_ENTRIES = "SELECT " + _ENTRY_COLUMNS + " FROM entries WHERE id = ANY($1)"

# --- Fleet / agent access-control SQL (ADRs 0011-0012) ---------------------

SELECT_FLEET_BY_NAME = "SELECT 1 FROM fleets WHERE name = $1"
INSERT_FLEET = "INSERT INTO fleets (name) VALUES ($1) RETURNING id, name, created_at"
GET_FLEET = "SELECT id, name, created_at FROM fleets WHERE id = $1"
LIST_FLEETS = "SELECT id, name, created_at FROM fleets ORDER BY created_at"

GET_AGENT = "SELECT * FROM agents WHERE name = $1"
INSERT_AGENT = (
    "INSERT INTO agents (name, owner_alias, status, trust_level) "
    "VALUES ($1, $2, 'pending', 0) RETURNING *"
)
BACKFILL_AGENT_ALIAS = "UPDATE agents SET owner_alias = $2 WHERE name = $1 AND owner_alias IS NULL"
LIST_AGENTS = "SELECT * FROM agents ORDER BY name"
ACTIVATE_AGENT = (
    "UPDATE agents SET status = 'active', trust_level = $2, home_fleet_id = $3, "
    "activated_at = now() WHERE name = $1 RETURNING *"
)
SET_AGENT_LEVEL = "UPDATE agents SET trust_level = $2 WHERE name = $1 RETURNING *"
SET_AGENT_FLEET = "UPDATE agents SET home_fleet_id = $2 WHERE name = $1 RETURNING *"

FLIP_SUPERSEDED = """
UPDATE entries
SET state = 'superseded', superseded_by = $1
WHERE id = ANY($2) AND state = 'active'
"""

LOOKUP_STATE = "SELECT state FROM entries WHERE id = $1"

WITHDRAW = (
    "UPDATE entries SET state = 'withdrawn', withdrawn_reason = $2 "
    "WHERE id = $1 AND state = 'active' RETURNING " + _ENTRY_COLUMNS
)

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
    if filters.tags:
        add("tags @> ?", list(filters.tags))
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


def _valid_uuids(ids: list[str] | tuple[str, ...]) -> list[str]:
    """Keep only the IDs that parse as UUIDs (others simply don't exist)."""
    return [i for i in ids if _is_valid_uuid(i)]


class PgStore:
    """A ``Store`` backed by PostgreSQL + pgvector (ADR 0007).

    Construct with a DSN (the pool opens lazily on first use) and
    ``close()`` on process shutdown.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        """Lazily open (and cache) the connection pool."""
        if self._pool is None:
            self._pool = await make_pool(self._dsn)
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
    ) -> Entry:
        """Insert a new entry and flip any ``draft.supersedes`` targets
        to the ``superseded`` state (SPEC.md §4.1).

        The insert and the supersession flip run in a single transaction
        (m4): a crash between them must not leave a new entry whose
        supersession targets were never flipped. ``embedding_model``
        (SPEC.md §7) records which model produced ``embedding``.
        """
        entry_id = str(uuid.uuid4())
        occurred_at = draft.resolved_occurred_at()

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    INSERT_ENTRY,
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
                    draft.scope,
                    draft.fleet_id,
                    embedding,
                    embedding_model,  # SPEC.md §7: model that produced the vector
                )
                if draft.supersedes:
                    targets = _valid_uuids(draft.supersedes)
                    if targets:
                        await conn.execute(FLIP_SUPERSEDED, entry_id, targets)
            row = await conn.fetchrow(SELECT_ENTRY, entry_id)
        assert row is not None
        return _row_to_entry(row)

    # -- read path ---------------------------------------------------------------

    async def get_entry(self, entry_id: str) -> Entry | None:
        if not _is_valid_uuid(entry_id):
            return None
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(SELECT_ENTRY, entry_id)
        return _row_to_entry(row) if row is not None else None

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        ids = _valid_uuids(entry_ids)
        if not ids:
            return {}
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(SELECT_ENTRIES, ids)
        return {str(row["id"]): _row_to_entry(row) for row in rows}

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
        async with pool.acquire() as conn:
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
        async with pool.acquire() as conn:
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
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(LOOKUP_STATE, entry_id)
            if existing is None:
                raise KeyError(f"unknown entry: {entry_id}")
            if existing["state"] != "active":
                raise ValueError(f"entry {entry_id} is {existing['state']}, not active")
            row = await conn.fetchrow(WITHDRAW, entry_id, reason)
        assert row is not None
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
        (SPEC.md §6.2 keyword stream). ``plainto_tsquery`` tokenizes the
        query safely (no operator injection). When ``visibility`` is
        supplied, only visible entries are candidates (ADR 0011)."""
        clauses, params = _filter_conditions(filters, visibility)
        query_idx = len(params) + 1
        clauses.append(f"search_tsv @@ plainto_tsquery('english', ${query_idx})")
        params.append(query)
        where = " AND ".join(clauses) if clauses else "TRUE"
        sql = (
            "SELECT id FROM entries WHERE "
            + where
            + f" ORDER BY ts_rank(search_tsv, plainto_tsquery('english', ${query_idx})) DESC,"
            + " created_at DESC, id DESC"
            + f" LIMIT ${query_idx + 1}"
        )
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
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
        candidates (ADR 0011)."""
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
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params, embedding, limit)
        return [str(r["id"]) for r in rows]

    # -- feedback -----------------------------------------------------------------

    async def record_feedback(self, feedback: Feedback) -> None:
        """Upsert a feedback verdict (one row per entry+user+agent)."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
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
        async with pool.acquire() as conn:
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
        async with pool.acquire() as conn:
            rows = await conn.fetch(BATCH_FEEDBACK, ids)
        counts = dict.fromkeys(ids, (0, 0, 0))
        for r in rows:
            counts[str(r["entry_id"])] = (r["helpful"], r["stale"], r["wrong"])
        return counts

    # -- fleets (ADR 0011) ------------------------------------------------------

    async def create_fleet(self, name: str) -> Fleet:
        """Create a named fleet (ADR 0011). ``ValueError`` if the name
        already exists."""
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            dup = await conn.fetchrow(SELECT_FLEET_BY_NAME, name)
            if dup is not None:
                raise ValueError(f"fleet already exists: {name!r}")
            row = await conn.fetchrow(INSERT_FLEET, name)
        assert row is not None
        return _row_to_fleet(row)

    async def list_fleets(self) -> list[Fleet]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(LIST_FLEETS)
        return [_row_to_fleet(r) for r in rows]

    async def get_fleet(self, fleet_id: str) -> Fleet | None:
        if not _is_valid_uuid(fleet_id):
            return None
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(GET_FLEET, fleet_id)
        return _row_to_fleet(row) if row is not None else None

    # -- agent registration / activation (ADR 0012) ----------------------------

    async def register_agent(self, name: str, owner_alias: str | None = None) -> Agent:
        """Register (or re-register) an agent (ADR 0012). Idempotent: an
        existing record is returned (only ``owner_alias`` back-filled if it
        was missing); a new record is ``pending`` (level 0, no fleet).
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                row = await conn.fetchrow(INSERT_AGENT, name, owner_alias)
            else:
                if owner_alias is not None:
                    await conn.execute(BACKFILL_AGENT_ALIAS, name, owner_alias)
                row = await conn.fetchrow(GET_AGENT, name)
        assert row is not None
        return _row_to_agent(row)

    async def get_agent(self, name: str) -> Agent | None:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(GET_AGENT, name)
        return _row_to_agent(row) if row is not None else None

    async def list_agents(self) -> list[Agent]:
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(LIST_AGENTS)
        return [_row_to_agent(r) for r in rows]

    async def activate_agent(
        self, name: str, *, trust_level: TrustLevel, home_fleet_id: str
    ) -> Agent:
        """Activate a pending agent: set trust level + home fleet, flip to
        ``active`` (ADR 0012). ``KeyError`` if unknown.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                raise KeyError(f"unknown agent: {name}")
            row = await conn.fetchrow(ACTIVATE_AGENT, name, trust_level.value, home_fleet_id)
        assert row is not None
        return _row_to_agent(row)

    async def set_agent_trust_level(self, name: str, level: TrustLevel) -> Agent:
        """Promote/demote an agent's trust level (ADR 0011). ``KeyError``
        if unknown. Demotion to level 0 is *dormant* (still active, no
        access) — distinct from revocation (ADR 0012).
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                raise KeyError(f"unknown agent: {name}")
            row = await conn.fetchrow(SET_AGENT_LEVEL, name, level.value)
        assert row is not None
        return _row_to_agent(row)

    async def set_agent_home_fleet(self, name: str, fleet_id: str) -> Agent:
        """Re-parent an agent to a new home fleet (ADR 0011). The agent's
        earlier ``fleet``-scoped entries stay in the fleet they were written
        into (never re-parented). ``KeyError`` if unknown.
        """
        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(GET_AGENT, name)
            if existing is None:
                raise KeyError(f"unknown agent: {name}")
            row = await conn.fetchrow(SET_AGENT_FLEET, name, fleet_id)
        assert row is not None
        return _row_to_agent(row)


# --- mappers --------------------------------------------------------------------


def _encode_sources(sources: tuple[Source, ...]) -> list[dict[str, str]]:
    """``sources`` is a jsonb column: a JSON list of {type, ref} objects."""
    return [{"type": source.type.value, "ref": source.ref} for source in sources]


def _decode_sources(raw: Any) -> tuple[Source, ...]:
    """Decode the jsonb ``sources`` column back into ``Source`` objects.

    asyncpg decodes ``jsonb`` to native Python, so ``raw`` is a list (or
    None); the JSON-string branch is kept for raw-text drivers.
    """
    if not raw:
        return ()
    items = raw if isinstance(raw, list) else json.loads(raw)
    return tuple(Source(type=SourceType(item["type"]), ref=item["ref"]) for item in items)


def _embedding_to_tuple(embedding: Any) -> tuple[float, ...] | None:
    """Normalize a stored embedding back into a float tuple.

    ``register_vector`` decodes the ``vector`` column into a
    ``pgvector.Vector``; the plain-list branch keeps the mapper robust
    if the codec returns a list instead.
    """
    if embedding is None:
        return None
    values = embedding.to_list() if hasattr(embedding, "to_list") else list(embedding)
    return tuple(float(x) for x in values)


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
        scope=row["scope"],
        fleet_id=str(row["fleet_id"]) if row["fleet_id"] else None,
        embedding=_embedding_to_tuple(row["embedding"]),
        embedding_model=row["embedding_model"],
        state=EntryState(row["state"]),
        superseded_by=str(row["superseded_by"]) if row["superseded_by"] else None,
        withdrawn_reason=row["withdrawn_reason"],
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
