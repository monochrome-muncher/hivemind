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
    occurred_at, author, agent, importance, scope,
    embedding, embedding_model
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14
)
"""
# 14 explicit columns/values; ``created_at`` falls back to the schema's
# ``now()`` default, so it is not in the value list.

_ENTRY_COLUMNS = """
    id, kind, summary, body, payload, sources, tags, occurred_at,
    created_at, author, agent, importance, scope, embedding,
    embedding_model, state, superseded_by, withdrawn_reason
"""

SELECT_ENTRY = "SELECT " + _ENTRY_COLUMNS + " FROM entries WHERE id = $1"

SELECT_ENTRIES = "SELECT " + _ENTRY_COLUMNS + " FROM entries WHERE id = ANY($1)"

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


def _filter_conditions(filters: EntryFilters) -> tuple[list[str], list[Any]]:
    """Translate an ``EntryFilters`` into WHERE-fragments + parameters.

    Mirrors ``EntryFilters.matches`` exactly (SPEC.md §5.3): by default
    only active entries are visible; ``include_inactive`` lifts the
    state restriction. Tags use AND-semantics (``tags @> $n``: the
    entry must carry every listed tag).
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

    return clauses, params


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

    async def create_entry(self, draft: EntryDraft, embedding: list[float] | None = None) -> Entry:
        """Insert a new entry and flip any ``draft.supersedes`` targets
        to the ``superseded`` state (SPEC.md §4.1).

        The new entry's ``id`` is a uuid4 assigned here and
        ``created_at`` is the database's ``now()``; the row is read
        back so the returned ``Entry`` matches what is stored.
        """
        entry_id = str(uuid.uuid4())
        occurred_at = draft.resolved_occurred_at()

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
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
                embedding,
                None,  # embedding_model: tracked at the embedder layer in v1
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
        self, filters: EntryFilters, limit: int = 20, offset: int = 0
    ) -> list[Entry]:
        """Filter-only listing (SPEC.md §5.1 ``GET /v1/entries``)."""
        clauses, params = _filter_conditions(filters)
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

    async def withdraw_entry(self, entry_id: str, reason: str | None, by_user: str) -> Entry:
        """Flip an entry to ``withdrawn`` (SPEC.md §4.1).

        Raises ``KeyError`` if the entry does not exist and
        ``ValueError`` if it is not active (the port contract).
        ``by_user`` is the audit record of who withdrew it; v1 records
        the reason but not the withdrawer (SPEC.md §11).
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

    async def search_keyword(self, query: str, filters: EntryFilters, limit: int) -> list[str]:
        """Ranked entry IDs by ``ts_rank`` over the maintained tsvector
        (SPEC.md §6.2 keyword stream). ``plainto_tsquery`` tokenizes the
        query safely (no operator injection)."""
        clauses, params = _filter_conditions(filters)
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
        self, embedding: list[float], filters: EntryFilters, limit: int
    ) -> list[str]:
        """Ranked entry IDs by cosine distance to ``embedding``
        (SPEC.md §6.2 vector stream; the pgvector ``<=>`` operator)."""
        clauses, params = _filter_conditions(filters)
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
        embedding=_embedding_to_tuple(row["embedding"]),
        embedding_model=row["embedding_model"],
        state=EntryState(row["state"]),
        superseded_by=str(row["superseded_by"]) if row["superseded_by"] else None,
        withdrawn_reason=row["withdrawn_reason"],
    )
