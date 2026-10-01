"""Search service — the retrieval pipeline (SPEC.md §6).

Keyword + vector streams, RRF fusion, decay-aware rescoring and the
supersession ranking invariant. Orchestration only: the math lives in
``hivemind.retrieval``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from hivemind.config import SearchConfig
from hivemind.domain.access import Visibility
from hivemind.domain.entry import (
    Entry,
    EntryFilters,
    EntryState,
    Kind,
)
from hivemind.domain.feedback import FeedbackCounts
from hivemind.domain.validation import check_pagination, check_query
from hivemind.embeddings import EmbeddingError
from hivemind.ports import Embedder, Store
from hivemind.retrieval.rrf import rrf_fuse
from hivemind.retrieval.scoring import entry_score, feedback_quality


@dataclass(frozen=True, slots=True)
class Hit:
    """A compact search hit, no ``body`` (SPEC.md §6.1: progressive
    disclosure)."""

    entry_id: str
    kind: Kind
    summary: str
    tags: tuple[str, ...]
    author: str
    agent: str
    occurred_at: datetime
    score: float
    # Lets a privileged reader spot a foreign fleet's entry (ADR 0036).
    scope: str = "fleet"
    fleet_id: str | None = None
    # (helpful, stale, wrong) over every reporter (ADR 0051).
    feedback: FeedbackCounts = (0, 0, 0)


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A page of hits; ``degraded`` when the embedder failed and the page
    is keyword-only (ADR 0048)."""

    hits: list[Hit]
    degraded: bool = False


NowFn = Callable[[], datetime]

logger = logging.getLogger(__name__)


async def _gather[A, B](first: Awaitable[A], second: Awaitable[B]) -> tuple[A, B]:
    """Run two awaitables concurrently. The first failure propagates
    unchanged (no ``ExceptionGroup``, unlike ``TaskGroup``) and the sibling
    is cancelled (unlike a bare ``gather``).
    """
    task_a = asyncio.ensure_future(first)
    task_b = asyncio.ensure_future(second)
    try:
        return await asyncio.gather(task_a, task_b)
    except BaseException:
        for task in (task_a, task_b):
            task.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
        raise


def _utcnow() -> datetime:
    return datetime.now(UTC)


class SearchService:
    """Orchestrates hybrid search over the store (SPEC.md §6.2/§6.3/§6.4)."""

    def __init__(
        self,
        store: Store,
        embedder: Embedder,
        config: SearchConfig,
        *,
        now_fn: NowFn | None = None,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._config = config
        self._now_fn: NowFn = now_fn or _utcnow

    async def search(
        self,
        query: str,
        filters: EntryFilters | None = None,
        limit: int | None = None,
        offset: int | None = None,
        *,
        visibility: Visibility | None = None,
    ) -> list[Hit]:
        """``search_result`` without the degraded flag: the hits only."""
        result = await self.search_result(query, filters, limit, offset, visibility=visibility)
        return result.hits

    async def search_result(
        self,
        query: str,
        filters: EntryFilters | None = None,
        limit: int | None = None,
        offset: int | None = None,
        *,
        visibility: Visibility | None = None,
    ) -> SearchResult:
        """Run a hybrid query and return compact hits, best first.

        If the embedder fails the result is keyword-only and ``degraded``
        (ADR 0048), not an error. A caller's first-page search is counted
        per home fleet (ADR 0056).
        """
        result = await self._ranked(query, filters, limit, offset, visibility)
        if visibility is not None and not offset:
            # An empty degraded answer reflects the embedder, not the pool.
            empty = not result.hits and not result.degraded
            await self._count_search(visibility, empty=empty)
        return result

    async def _count_search(self, visibility: Visibility, *, empty: bool) -> None:
        """Best-effort (ADR 0056): a failed count never fails the search."""
        try:
            await self._store.record_search(visibility.home_fleet_id, empty=empty)
        except Exception as exc:
            logger.warning("search count not recorded: %s", type(exc).__name__)

    async def _ranked(
        self,
        query: str,
        filters: EntryFilters | None,
        limit: int | None,
        offset: int | None,
        visibility: Visibility | None,
    ) -> SearchResult:
        """The search itself (``search_result`` without the counting)."""
        # Validate before any embedder or store call (ADR 0040).
        check_query(query)
        check_pagination(limit, offset)
        filters = filters or EntryFilters()
        filters.validate()
        limit = limit if limit is not None else self._config.default_limit
        offset = offset or 0
        top_k = self._config.candidate_top_k

        # 1. Both streams concurrently (PERF-4); ``vector_ids`` is None when
        # the embedder failed (ADR 0048).
        keyword_ids, vector_ids = await _gather(
            self._store.search_keyword(query, filters, top_k, visibility=visibility),
            self._vector_stream(query, filters, top_k, visibility),
        )

        # 2. RRF fusion (configurable weights, SPEC.md §6.2).
        degraded = vector_ids is None
        fused = rrf_fuse(
            [keyword_ids, vector_ids or []],
            k=self._config.rrf_k,
            weights=[self._config.weight_keyword, self._config.weight_vector],
        )
        if not fused:
            return SearchResult([], degraded)

        # 3. Fetch candidates + batched feedback counts.
        candidate_ids = list(fused)
        entries, counts = await _gather(
            self._store.get_entries(candidate_ids),
            self._store.quality_counts(candidate_ids),
        )
        quality_kwargs = self._config.quality_kwargs()

        # 4. Decay-aware rescore (SPEC.md §6.4).
        now = self._now_fn()
        scored: list[Entry] = []
        entry_scores: dict[str, float] = {}
        entry_feedback: dict[str, FeedbackCounts] = {}
        # Fused-rank order, not row order: ties are common and the stable
        # sorts below keep this order (SP-5).
        for entry in (entries[eid] for eid in candidate_ids if eid in entries):
            if not filters.include_inactive and entry.state is not EntryState.ACTIVE:
                continue
            entry_counts: FeedbackCounts = counts.get(entry.id, (0, 0, 0))
            entry_feedback[entry.id] = entry_counts
            helpful, stale, wrong = entry_counts
            quality = feedback_quality(helpful, stale, wrong, **quality_kwargs)
            score = entry_score(
                fused[entry.id],
                entry.importance,
                entry.occurred_at,
                quality,
                now,
                self._config.half_life_days,
                recency_floor=self._config.recency_floor,
            )
            entry_scores[entry.id] = score
            scored.append(entry)

        # 5. Supersession ranking invariant (SPEC.md §6.3).
        ordered = apply_supersession_invariant(scored, entry_scores)

        # 6. Pagination (SPEC.md §5.3: limit/offset on search and list).
        hits = [
            self._to_hit(entry, entry_scores[entry.id], entry_feedback[entry.id])
            for entry in ordered[offset : offset + limit]
        ]
        return SearchResult(hits, degraded)

    async def _vector_stream(
        self,
        query: str,
        filters: EntryFilters,
        top_k: int,
        visibility: Visibility | None,
    ) -> list[str] | None:
        """The vector stream, or None on an ``EmbeddingError`` (ADR 0048);
        store failures propagate."""
        try:
            query_vector = await self._embedder.embed_text(query)
        except EmbeddingError as exc:
            # Already sanitized (providers.py); the caller sees no error.
            logger.warning("embedding unavailable, searching by keyword only: %s", exc)
            return None
        return await self._store.search_vector(query_vector, filters, top_k, visibility=visibility)

    def _to_hit(self, entry: Entry, score: float, feedback: FeedbackCounts) -> Hit:
        return Hit(
            entry_id=entry.id,
            kind=entry.kind,
            summary=entry.summary,
            tags=entry.tags,
            author=entry.author,
            agent=entry.agent,
            occurred_at=entry.occurred_at,
            score=score,
            scope=entry.scope,
            fleet_id=entry.fleet_id,
            feedback=feedback,
        )


def apply_supersession_invariant(
    entries: list[Entry],
    scores: dict[str, float],
) -> list[Entry]:
    """Enforce: within a supersession chain, the newest entry outranks all
    predecessors (SPEC.md §6.3).

    Group by chain head, order groups by their best score, and within a
    group newest ``created_at`` first. Ties keep the order of ``entries``
    (stable sorts; SP-5).
    """
    successor_of = {e.id: e.superseded_by for e in entries}
    id_set = set(successor_of)

    # Follow superseded_by until the link leaves the candidate set.
    def chain_head(entry: Entry) -> str:
        head = entry
        guard = 0
        while head.superseded_by in id_set:
            head = next(
                (e for e in entries if e.id == head.superseded_by),
                head,
            )
            guard += 1
            if guard > 1000:  # defensive: chains are short in practice
                break
        return head.id

    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(chain_head(entry), []).append(entry)

    scored_groups: list[_ScoredGroup] = []
    for members in groups.values():
        members.sort(key=lambda e: e.created_at, reverse=True)
        head_score = max(scores[m.id] for m in members)
        scored_groups.append(_ScoredGroup(key=head_score, members=members))
    scored_groups.sort(key=lambda g: g.key, reverse=True)

    result: list[Entry] = []
    for group in scored_groups:
        result.extend(group.members)
    return result


@dataclass
class _ScoredGroup:
    key: float
    members: list[Entry] = field(default_factory=list)
