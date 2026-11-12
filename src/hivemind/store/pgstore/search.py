"""Keyword and vector search, similar entries and the search counters."""

from __future__ import annotations

import math

from hivemind.domain.access import Visibility
from hivemind.domain.entry import EntryFilters, SearchCount
from hivemind.domain.validation import is_valid_uuid
from hivemind.ports import VectorMatch
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.sql_common import VECTOR_SEARCH_SETTINGS, filter_conditions
from hivemind.store.pgstore.uuids import valid_uuids

# ADR 0074: every candidate pair from the stored vectors (no embedder call); rows without one drop
# out.
PAIRWISE_SIMILARITY = (
    "SELECT a.id AS a, b.id AS b, 1 - (a.embedding <=> b.embedding) AS similarity "
    "FROM entries a JOIN entries b ON a.id < b.id "
    "WHERE a.id = ANY($1) AND b.id = ANY($1) "
    "AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL"
)

# One row per home fleet ('' = none), bumped per first-page search (ADR 0056); counts only, no query
# text.
RECORD_SEARCH = """
INSERT INTO search_counts (fleet_id, searches, empty, explore) VALUES ($1, 1, $2, $3)
ON CONFLICT (fleet_id) DO UPDATE
   SET searches = search_counts.searches + 1,
       empty = search_counts.empty + EXCLUDED.empty,
       explore = search_counts.explore + EXCLUDED.explore
"""

SEARCH_COUNTS = "SELECT fleet_id, searches, empty, explore FROM search_counts"

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


class SearchStore(PgConnections):
    async def pairwise_similarity(self, entry_ids: list[str]) -> dict[tuple[str, str], float]:
        ids = valid_uuids(list(dict.fromkeys(entry_ids)))
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

    async def search_keyword(
        self,
        query: str,
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
    ) -> list[str]:
        """Ranked entry IDs by ``ts_rank`` (SPEC.md §6.2 keyword stream; ``ANY_TERM_TSQUERY``)."""
        clauses, params = filter_conditions(filters, visibility)
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
    ) -> list[VectorMatch]:
        """Ranked entries with their cosine similarity (SPEC.md §6.2), approximate via the HNSW index
        (ADR 0025) under ``VECTOR_SEARCH_SETTINGS``. Entries at or below ``min_similarity`` are
        dropped after the index scan: they sort last, so the result is the same (ADRs 0061, 0062)."""
        clauses, params = filter_conditions(filters, visibility)
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
        matches = [VectorMatch(str(r["id"]), 1.0 - r["distance"]) for r in rows]
        return [m for m in matches if m.similarity > min_similarity]

    async def similar_entries(
        self,
        embedding: list[float],
        limit: int,
        *,
        exclude_id: str | None = None,
        visibility: Visibility | None = None,
    ) -> list[tuple[str, float]]:
        """The active entries nearest to ``embedding`` with their cosine similarity (ADR 0052);
        approximate like ``search_vector`` (ADR 0025)."""
        clauses, params = filter_conditions(EntryFilters(), visibility)
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
        # pgvector gives NaN for a zero vector; MemoryStore says 0.0.
        return [
            (str(r["id"]), s if math.isfinite(s := float(r["similarity"])) else 0.0) for r in rows
        ]
