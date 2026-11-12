"""Keyword and vector search, similar entries and the search counters."""

from __future__ import annotations

import uuid
from datetime import datetime

from hivemind.domain.access import Visibility
from hivemind.domain.entry import EntryFilters, SearchCount, embeddable_text
from hivemind.domain.validation import is_valid_uuid
from hivemind.memstore.helpers import all_pairs_cosine, canon, cosine_similarity
from hivemind.memstore.state import MemoryState
from hivemind.ports import VectorMatch


def _tokens(text: str) -> set[str]:
    return set(text.lower().split())


class SearchStore(MemoryState):
    async def search_keyword(
        self, query: str, filters: EntryFilters, limit: int, *, visibility: Visibility | None = None
    ) -> list[str]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        with self._lock:
            rows: list[tuple[float, datetime, str]] = []
            for entry in self._entries.values():
                if not self._matches(entry, filters) or not self._visible(entry, visibility):
                    continue
                if entry.redacted_at is not None:  # no keyword index left (ADR 0125)
                    continue
                # Same text the vector stream sees, at the default budget
                # (ADR 0021; this store has no Settings).
                haystack = _tokens(embeddable_text(entry.summary, entry.body)) | {
                    tag.lower() for tag in entry.tags
                }
                overlap = len(query_tokens & haystack)
                if overlap == 0:
                    continue
                rows.append((float(overlap), entry.created_at, entry.id))
            # Ties newest first, as PgStore's ``created_at DESC, id DESC``.
            rows.sort(key=lambda r: (r[1], r[2]), reverse=True)
            rows.sort(key=lambda r: -r[0])
            return [eid for _, _, eid in rows[:limit]]

    async def search_vector(
        self,
        vector: list[float],
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
        min_similarity: float = 0.0,
    ) -> list[VectorMatch]:
        with self._lock:
            rows: list[tuple[float, datetime, str]] = []
            for entry in self._entries.values():
                if not self._matches(entry, filters) or not self._visible(entry, visibility):
                    continue
                if entry.embedding is None:
                    continue
                sim = cosine_similarity(vector, list(entry.embedding))
                if sim <= min_similarity:  # ADRs 0061, 0062
                    continue
                rows.append((sim, entry.created_at, entry.id))
            rows.sort(key=lambda r: (-r[0], r[1], r[2]))
            return [VectorMatch(eid, sim) for sim, _, eid in rows[:limit]]

    async def similar_entries(
        self,
        embedding: list[float],
        limit: int,
        *,
        exclude_id: str | None = None,
        visibility: Visibility | None = None,
    ) -> list[tuple[str, float]]:
        active = EntryFilters()
        excluded = canon(exclude_id) if exclude_id is not None else None
        with self._lock:
            rows: list[tuple[float, datetime, str]] = []
            for entry in self._entries.values():
                if (excluded is not None and entry.id == excluded) or entry.embedding is None:
                    continue
                if not active.matches(entry) or not self._visible(entry, visibility):
                    continue
                sim = cosine_similarity(embedding, list(entry.embedding))
                rows.append((sim, entry.created_at, entry.id))
            rows.sort(key=lambda r: (-r[0], r[1], r[2]))
            return [(eid, sim) for sim, _, eid in rows[:limit]]

    async def pairwise_similarity(self, entry_ids: list[str]) -> dict[tuple[str, str], float]:
        with self._lock:
            vectors = {
                key: list(entry.embedding)
                for eid in entry_ids
                if is_valid_uuid(eid)
                and (key := str(uuid.UUID(eid))) is not None
                and (entry := self._entries.get(key)) is not None
                and entry.embedding is not None
            }
        return all_pairs_cosine(sorted(vectors), vectors)

    async def record_search(
        self, fleet_id: str | None, *, empty: bool, explore: bool = False
    ) -> None:
        with self._lock:
            searches, misses, explored = self._searches.get(fleet_id, (0, 0, 0))
            self._searches[fleet_id] = (searches + 1, misses + int(empty), explored + int(explore))

    async def search_counts(self) -> list[SearchCount]:
        with self._lock:
            return [SearchCount(f, s, e, x) for f, (s, e, x) in self._searches.items()]
