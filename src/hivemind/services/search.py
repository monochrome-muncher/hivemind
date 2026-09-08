"""Search service — the retrieval pipeline (SPEC.md §6).

This is the deep module of the read path: one small interface
(``search``) over a deep interior — dual-stream retrieval (keyword +
vector), RRF fusion (configurable weights), decay-aware rescoring
(similarity x importance x recency x feedback quality), and the
supersession ranking invariant (a successor always outranks the
entry it superseded; superseded/withdrawn entries hidden unless
``include_inactive``).

Everything here is orchestration: the pure math lives in
``hivemind.retrieval``, and all I/O happens through the ``Store``
and ``Embedder`` ports.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from hivemind.config import SearchConfig
from hivemind.domain.entry import (
    Entry,
    EntryFilters,
    EntryState,
    Kind,
)
from hivemind.domain.feedback import FeedbackCounts
from hivemind.ports import Embedder, Store
from hivemind.retrieval.rrf import rrf_fuse
from hivemind.retrieval.scoring import entry_score, feedback_quality


@dataclass(frozen=True, slots=True)
class Hit:
    """A compact search hit (SPEC.md §6.1: progressive disclosure).

    Deliberately carries no ``body``: agents scan compact hits and
    open the interesting ones via the get endpoint.
    """

    entry_id: str
    kind: Kind
    summary: str
    tags: tuple[str, ...]
    author: str
    agent: str
    occurred_at: datetime
    score: float


NowFn = Callable[[], datetime]


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
    ) -> list[Hit]:
        """Run a hybrid query and return compact hits, best first.

        Pipeline: keyword + vector streams (each ``candidate_top_k``)
        -> RRF fusion -> decay-aware rescore -> supersession invariant
        -> pagination (``offset`` then ``limit``; SPEC.md §5.3).
        Superseded/withdrawn entries are excluded unless
        ``filters.include_inactive`` is set.
        """
        filters = filters or EntryFilters()
        limit = limit if limit is not None else self._config.default_limit
        offset = offset or 0
        top_k = self._config.candidate_top_k

        # 1. Dual-stream retrieval.
        keyword_ids = await self._store.search_keyword(query, filters, top_k)
        query_vector = await self._embedder.embed_text(query)
        vector_ids = await self._store.search_vector(query_vector, filters, top_k)

        # 2. RRF fusion (configurable weights, SPEC.md §6.2).
        fused = rrf_fuse(
            [keyword_ids, vector_ids],
            k=self._config.rrf_k,
            weights=[self._config.weight_keyword, self._config.weight_vector],
        )
        if not fused:
            return []

        # 3. Fetch candidates + batched feedback counts.
        candidate_ids = list(fused)
        entries = await self._store.get_entries(candidate_ids)
        counts = await self._store.quality_counts(candidate_ids)
        quality_kwargs = self._config.quality_kwargs()

        # 4. Decay-aware rescore (SPEC.md §6.4).
        now = self._now_fn()
        scored: list[Entry] = []
        entry_scores: dict[str, float] = {}
        for entry in entries.values():
            if not filters.include_inactive and entry.state is not EntryState.ACTIVE:
                continue
            entry_counts: FeedbackCounts = counts.get(entry.id, (0, 0, 0))
            helpful, stale, wrong = entry_counts
            quality = feedback_quality(helpful, stale, wrong, **quality_kwargs)
            score = entry_score(
                fused[entry.id],
                entry.importance,
                entry.occurred_at,
                quality,
                now,
                self._config.half_life_days,
            )
            entry_scores[entry.id] = score
            scored.append(entry)

        # 5. Supersession ranking invariant (SPEC.md §6.3).
        ordered = apply_supersession_invariant(scored, entry_scores)

        # 6. Pagination (SPEC.md §5.3: limit/offset on search and list).
        return [
            self._to_hit(entry, entry_scores[entry.id])
            for entry in ordered[offset : offset + limit]
        ]

    def _to_hit(self, entry: Entry, score: float) -> Hit:
        return Hit(
            entry_id=entry.id,
            kind=entry.kind,
            summary=entry.summary,
            tags=entry.tags,
            author=entry.author,
            agent=entry.agent,
            occurred_at=entry.occurred_at,
            score=score,
        )


def apply_supersession_invariant(
    entries: list[Entry],
    scores: dict[str, float],
) -> list[Entry]:
    """Enforce: within a supersession chain, the newest entry outranks all
    predecessors (SPEC.md §6.3).

    Deterministic rule: group entries by their chain head (the newest
    entry in the chain); order groups by the head's score descending;
    within a group, entries are ordered by ``created_at`` descending
    (newest first). Single-entry groups are unaffected.
    """
    # Map: entry_id -> successor_id (a superseded entry points at its successor).
    successor_of = {e.id: e.superseded_by for e in entries}
    id_set = set(successor_of)

    # Chain head = follow superseded_by until the link leaves the candidate set
    # or the entry is not superseded.
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
