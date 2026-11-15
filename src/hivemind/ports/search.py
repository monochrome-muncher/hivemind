"""The keyword and vector retrieval streams and the search counters."""

from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable

from hivemind.domain.access import (
    Visibility,
)
from hivemind.domain.entry import (
    EntryFilters,
    SearchCount,
)


class VectorMatch(NamedTuple):
    """One vector-stream candidate: the entry and its cosine similarity to the query."""

    entry_id: str
    similarity: float


@runtime_checkable
class SearchPort(Protocol):
    """The keyword and vector retrieval streams and the search counters."""

    async def pairwise_similarity(self, entry_ids: list[str]) -> dict[tuple[str, str], float]:
        """Cosine similarity of every pair among ``entry_ids`` (ADR 0074), keyed ``(a, b)`` with ``a
        < b`` as strings.

        A pair is absent when either entry is unknown or has no vector. Stored vectors only (no
        embedder call); not filtered by visibility.
        """
        ...

    async def record_search(
        self, fleet_id: str | None, *, empty: bool, explore: bool = False
    ) -> None:
        """Count one first-page search from ``fleet_id`` (``None`` = no home fleet), empty (ADR
        0056) or ``explore`` (ADR 0078). Counts only: no query text, no agent name."""
        ...

    async def search_counts(self) -> list[SearchCount]:
        """The search counters, one row per home fleet, any order."""
        ...

    async def search_keyword(
        self, query: str, filters: EntryFilters, limit: int, *, visibility: Visibility | None = None
    ) -> list[str]:
        """Entry IDs by keyword relevance, descending; ``visibility`` as in ``list_entries``."""
        ...

    async def search_vector(
        self,
        embedding: list[float],
        filters: EntryFilters,
        limit: int,
        *,
        visibility: Visibility | None = None,
        min_similarity: float = 0.0,
    ) -> list[VectorMatch]:
        """Entries by embedding similarity, descending, each with its cosine similarity, leaving out
        those <= ``min_similarity`` (0 by default, ADR 0061; ADR 0062); ``visibility`` as in
        ``list_entries``."""
        ...

    async def similar_entries(
        self,
        embedding: list[float],
        limit: int,
        *,
        exclude_id: str | None = None,
        visibility: Visibility | None = None,
    ) -> list[tuple[str, float]]:
        """Active entries nearest ``embedding`` as ``(id, cosine similarity)``, highest first, minus
        ``exclude_id`` (ADR 0052) and limited to ``visibility`` when given.

        Approximate on Postgres, like ``search_vector`` (ADR 0025)."""
        ...
