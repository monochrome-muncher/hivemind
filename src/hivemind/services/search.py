"""Search service: the retrieval pipeline (SPEC.md §6). Orchestration only; the math is in
``hivemind.retrieval``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from hivemind.config import SearchConfig
from hivemind.domain.access import SCOPE_FLEET, SCOPE_ORG, Visibility
from hivemind.domain.entry import (
    Entry,
    EntryFilters,
    EntryState,
    Kind,
)
from hivemind.domain.feedback import (
    NO_SIGNALS,
    Correction,
    FeedbackCounts,
    FeedbackSignals,
    is_contested,
    shown_correction,
)
from hivemind.domain.recency import RecencyHint
from hivemind.domain.upstream import UPSTREAM_KINDS, UpstreamChange, upstream_changed
from hivemind.domain.validation import check_pagination, check_query, utcnow
from hivemind.embeddings import EmbeddingError
from hivemind.ports import Embedder, Store
from hivemind.retrieval.mmr import mmr_order
from hivemind.retrieval.rrf import rrf_fuse
from hivemind.retrieval.scoring import entry_score, feedback_quality
from hivemind.services.ownership import own_author_filter


@dataclass(frozen=True, slots=True)
class SimilarEntry:
    """Another member of a hit's version group (ADR 0074)."""

    entry_id: str
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class Hit:
    """A compact search hit, no ``body`` (SPEC.md §6.1)."""

    entry_id: str
    kind: Kind
    summary: str
    tags: tuple[str, ...]
    author: str
    agent: str
    occurred_at: datetime
    score: float
    # Lets a privileged reader spot a foreign fleet's entry (ADR 0036).
    scope: str = SCOPE_FLEET
    fleet_id: str | None = None
    # (helpful, stale, wrong) over every reporter (ADR 0051).
    feedback: FeedbackCounts = (0, 0, 0)
    # Last confirmation by a non-author with no flag since (SPEC §4.2); display only, never ranked.
    last_confirmed_at: datetime | None = None
    # Evidence on the hit (SPEC §6.1), display only. ``state`` only under ``include_inactive``.
    contested: bool = False
    correction: Correction | None = None
    supersedes: int = 0
    state: EntryState | None = None
    # Unlinked near-copies among the candidates, newest first (ADR 0074); display only.
    similar: tuple[SimilarEntry, ...] = ()
    # see_also targets changed since the link, at most 3 (ADR 0081); display only.
    upstream_changed: tuple[UpstreamChange, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A page of hits; ``degraded`` when the page is keyword-only (ADR 0048)."""

    hits: list[Hit]
    degraded: bool = False
    # The hint the scores were computed under (ADR 0073).
    recency: RecencyHint = RecencyHint.DEFAULT


@dataclass(frozen=True, slots=True)
class _ScoredCandidates:
    """State-filtered candidates in fused-rank order, with scores and feedback signals."""

    entries: list[Entry]
    scores: dict[str, float]
    feedback: dict[str, FeedbackSignals]


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


class SearchService:
    """Hybrid search over the store (SPEC.md §6.2 to §6.4)."""

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
        self._now_fn: NowFn = now_fn or utcnow

    async def search(
        self,
        query: str,
        filters: EntryFilters | None = None,
        limit: int | None = None,
        offset: int | None = None,
        *,
        visibility: Visibility | None = None,
        recency: RecencyHint = RecencyHint.DEFAULT,
        explore: bool = False,
    ) -> list[Hit]:
        """``search_result`` without the degraded flag: the hits only."""
        result = await self.search_result(
            query,
            filters,
            limit,
            offset,
            visibility=visibility,
            recency=recency,
            explore=explore,
        )
        return result.hits

    async def search_result(
        self,
        query: str,
        filters: EntryFilters | None = None,
        limit: int | None = None,
        offset: int | None = None,
        *,
        visibility: Visibility | None = None,
        recency: RecencyHint = RecencyHint.DEFAULT,
        explore: bool = False,
        count: bool = True,
    ) -> SearchResult:
        """Hybrid query, compact hits best first. Embedder failure gives a keyword-only ``degraded``
        result (ADR 0048); first pages are counted per home fleet (ADR 0056), ``count=False`` skips
        that (ADR 0080); ``recency`` (ADR 0073) and ``explore`` (ADR 0078) are per-call hints.
        """
        result = await self._ranked(query, filters, limit, offset, visibility, recency, explore)
        if count and visibility is not None and not offset:
            # An empty degraded answer reflects the embedder, not the pool.
            empty = not result.hits and not result.degraded
            await self._count_search(visibility, empty=empty, explore=explore)
        return result

    async def _count_search(
        self, visibility: Visibility, *, empty: bool, explore: bool = False
    ) -> None:
        """Best-effort (ADR 0056): a failed count never fails the search."""
        try:
            await self._store.record_search(visibility.home_fleet_id, empty=empty, explore=explore)
        except Exception as exc:
            logger.warning("search count not recorded: %s", type(exc).__name__)

    async def _ranked(
        self,
        query: str,
        filters: EntryFilters | None,
        limit: int | None,
        offset: int | None,
        visibility: Visibility | None,
        recency: RecencyHint,
        explore: bool = False,
    ) -> SearchResult:
        """``search_result`` without the counting."""
        # Validate before any embedder or store call (ADR 0040).
        check_query(query)
        check_pagination(limit, offset)
        filters = self._validated_filters(filters, visibility)
        limit = limit if limit is not None else self._config.default_limit
        offset = offset or 0
        applied = self._config.applied_hint(recency)

        fused, degraded = await self._fused_candidates(query, filters, visibility)
        if not fused:
            return SearchResult([], degraded, applied)

        entries, signals = await self._fetch_candidates(list(fused), visibility)
        scored = self._score_candidates(entries, signals, fused, filters, recency)
        # Run after the two concurrent reads above, never beside them (ADR 0059).
        ordered, similar = await self._rank_versions(scored, explore)
        page = ordered[offset : offset + limit]
        hits = await self._page_hits(page, scored, similar, filters, visibility)
        return SearchResult(hits, degraded, applied)

    @staticmethod
    def _validated_filters(
        filters: EntryFilters | None, visibility: Visibility | None
    ) -> EntryFilters:
        """The caller's filters, validated, with the own-author filter applied."""
        filters = filters or EntryFilters()
        filters.validate()
        if visibility is not None:
            filters = own_author_filter(filters, visibility)
        return filters

    async def _fused_candidates(
        self, query: str, filters: EntryFilters, visibility: Visibility | None
    ) -> tuple[dict[str, float], bool]:
        """RRF-fused candidate ids with scores (SPEC.md §6.2), and whether the vector stream was
        lost."""
        top_k = self._config.candidate_top_k
        # ``vector_ids`` is None when the embedder failed (ADR 0048).
        keyword_ids, vector_ids = await _gather(
            self._store.search_keyword(query, filters, top_k, visibility=visibility),
            self._vector_stream(query, filters, top_k, visibility),
        )
        fused = rrf_fuse(
            [keyword_ids, vector_ids or []],
            k=self._config.rrf_k,
            weights=[self._config.weight_keyword, self._config.weight_vector],
        )
        return fused, vector_ids is None

    async def _fetch_candidates(
        self, candidate_ids: list[str], visibility: Visibility | None
    ) -> tuple[Mapping[str, Entry], Mapping[str, FeedbackSignals]]:
        """The candidate entries and their batched feedback signals."""
        return await _gather(
            self._store.get_entries(candidate_ids),
            self._store.feedback_signals(
                candidate_ids, count_supersedes=True, visibility=visibility
            ),
        )

    def _score_candidates(
        self,
        entries: Mapping[str, Entry],
        signals: Mapping[str, FeedbackSignals],
        fused: Mapping[str, float],
        filters: EntryFilters,
        recency: RecencyHint,
    ) -> _ScoredCandidates:
        """Decay-aware rescore of the fused candidates (SPEC.md §6.4)."""
        now = self._now_fn()
        scored: list[Entry] = []
        entry_scores: dict[str, float] = {}
        entry_feedback: dict[str, FeedbackSignals] = {}
        # Fused-rank order, not row order: ties are common and the stable sorts below keep it.
        for entry in (entries[eid] for eid in fused if eid in entries):
            if not filters.include_inactive and entry.state is not EntryState.ACTIVE:
                continue
            entry_signals = signals.get(entry.id, NO_SIGNALS)
            entry_feedback[entry.id] = entry_signals
            helpful, stale, wrong = entry_signals.counts
            quality = feedback_quality(
                helpful,
                stale,
                wrong,
                **self._config.quality_kwargs(
                    confirmed=entry_signals.last_confirmed_at is not None
                ),
            )
            terms = self._config.recency_terms(recency)
            score = entry_score(
                fused[entry.id],
                entry.importance,
                entry.occurred_at,
                quality,
                now,
                terms.half_life_days,
                recency_floor=terms.recency_floor,
                social_max=self._config.social_max,
                narrow_importance=self._config.narrow_importance,
            )
            entry_scores[entry.id] = score
            scored.append(entry)
        return _ScoredCandidates(scored, entry_scores, entry_feedback)

    async def _rank_versions(
        self, candidates: _ScoredCandidates, explore: bool
    ) -> tuple[list[Entry], dict[str, tuple[SimilarEntry, ...]]]:
        """Display order: version groups (ADR 0074), supersession invariant (SPEC.md §6.3), opt-in
        explore re-order (ADR 0078); also each hit's similar-entry list."""
        scored = candidates.entries
        version_groups: list[list[Entry]] = []
        threshold = self._config.version_group_min_similarity
        shared = await self._shared_similarity(scored, explore)
        if threshold is not None:
            version_groups = await self._version_groups(
                scored, candidates.scores, candidates.feedback, threshold, shared
            )
        group_ids = [[m.id for m in g] for g in version_groups]
        ordered = apply_supersession_invariant(scored, candidates.scores, version_groups=group_ids)
        if explore and len(ordered) > 1:
            ordered = await self._explore_order(ordered, candidates.scores, group_ids, shared)
        return ordered, _similar_map(version_groups)

    async def _shared_similarity(
        self, scored: list[Entry], explore: bool
    ) -> dict[tuple[str, str], float] | None:
        """With grouping and explore both on, read the similarities once; a failed shared read
        leaves each step to read (and fail) on its own."""
        if self._config.version_group_min_similarity is None or not explore or len(scored) < 2:
            return None
        try:
            return await self._store.pairwise_similarity([e.id for e in scored])
        except Exception:
            logger.warning("shared similarity read failed", exc_info=True)
            return None

    async def _page_hits(
        self,
        page: list[Entry],
        candidates: _ScoredCandidates,
        similar: Mapping[str, tuple[SimilarEntry, ...]],
        filters: EntryFilters,
        visibility: Visibility | None,
    ) -> list[Hit]:
        """The hits for one page, with the upstream markers read for the page only."""
        upstream = await self._upstream_changes(page, visibility)
        return [
            self._to_hit(
                entry,
                candidates.scores[entry.id],
                candidates.feedback[entry.id],
                with_state=filters.include_inactive,
                similar=similar.get(entry.id, ()),
                upstream_changed=upstream.get(entry.id, ()),
            )
            for entry in page
        ]

    async def _upstream_changes(
        self, page: list[Entry], visibility: Visibility | None
    ) -> dict[str, tuple[UpstreamChange, ...]]:
        """``upstream_changed`` markers for the page (ADR 0081): one batched read after the gathered
        ones. Best-effort: a failed read means no marker, never a failed search."""
        wanted = [e for e in page if e.kind in UPSTREAM_KINDS]
        if not wanted:
            return {}
        try:
            links = await self._store.upstream_links([e.id for e in wanted], visibility=visibility)
        except Exception:
            logger.warning("upstream_changed skipped", exc_info=True)
            return {}
        return {e.id: upstream_changed(e.kind, e.author, links.get(e.id, [])) for e in wanted}

    async def _version_groups(
        self,
        scored: list[Entry],
        scores: dict[str, float],
        feedback: dict[str, FeedbackSignals],
        threshold: float,
        similarity: Mapping[tuple[str, str], float] | None = None,
    ) -> list[list[Entry]]:
        """Version groups among the best candidates (ADR 0074). Best-effort: a failed read means no
        groups, never a failed search. ``similarity``, when given, spares the read."""
        ranked = sorted(
            (e for e in scored if e.state is EntryState.ACTIVE),
            key=lambda e: scores[e.id],
            reverse=True,
        )[:MAX_GROUPED_CANDIDATES]
        if len(ranked) < 2:
            return []
        try:
            if similarity is None:
                similarity = await self._store.pairwise_similarity([e.id for e in ranked])
        except Exception as exc:
            # Logged with the traceback: a broken adapter would otherwise only
            # show as grouping that never happens.
            logger.warning("version grouping skipped: %s", type(exc).__name__, exc_info=True)
            return []
        return group_versions(ranked, similarity, threshold, feedback)

    async def _explore_order(
        self,
        ordered: list[Entry],
        scores: dict[str, float],
        version_groups: Sequence[Sequence[str]] = (),
        similarity: Mapping[tuple[str, str], float] | None = None,
    ) -> list[Entry]:
        """``ordered`` re-ordered by MMR (ADR 0078). Runs after the gathered reads (ADR 0059), so a
        search never has more than two store calls in flight. Best-effort: a failed read keeps the
        normal order. ``similarity``, when given, spares the read.
        """
        try:
            if similarity is None:
                similarity = await self._store.pairwise_similarity([e.id for e in ordered])
        except Exception:
            logger.warning("explore: similarity read failed, normal order kept", exc_info=True)
            return ordered
        return explore_reorder(
            ordered, scores, similarity, self._config.explore_lambda, version_groups
        )

    async def _vector_stream(
        self,
        query: str,
        filters: EntryFilters,
        top_k: int,
        visibility: Visibility | None,
    ) -> list[str] | None:
        """The vector stream, or None on an ``EmbeddingError`` (ADR 0048); store failures propagate.
        Only this stream carries the query instruction (ADR 0097)."""
        try:
            query_vector = await self._embedder.embed_text(self._config.embedded_query(query))
        except EmbeddingError as exc:
            # Already sanitized (providers.py); the caller sees no error.
            logger.warning("embedding unavailable, searching by keyword only: %s", exc)
            return None
        return await self._store.search_vector(
            query_vector,
            filters,
            top_k,
            visibility=visibility,
            min_similarity=self._config.vector_min_similarity or 0.0,
        )

    def _to_hit(
        self,
        entry: Entry,
        score: float,
        signals: FeedbackSignals,
        *,
        with_state: bool = False,
        similar: tuple[SimilarEntry, ...] = (),
        upstream_changed: tuple[UpstreamChange, ...] = (),
    ) -> Hit:
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
            feedback=signals.counts,
            last_confirmed_at=signals.last_confirmed_at,
            contested=is_contested(*signals.counts),
            correction=shown_correction(signals.correction, signals.last_confirmed_at),
            supersedes=signals.supersedes,
            state=entry.state if with_state else None,
            similar=similar,
            upstream_changed=upstream_changed,
        )


def chain_heads(entries: list[Entry]) -> dict[str, str]:
    """The chain head of each candidate id: follow ``superseded_by`` until the link leaves the
    set."""
    # The first one, should an id repeat.
    by_id: dict[str, Entry] = {}
    for entry in entries:
        by_id.setdefault(entry.id, entry)

    def chain_head(entry: Entry) -> str:
        head = entry
        guard = 0
        while head.superseded_by in by_id:
            head = by_id[head.superseded_by]
            guard += 1
            if guard > 1000:  # defensive: chains are short in practice
                break
        return head.id

    return {entry.id: chain_head(entry) for entry in entries}


def explore_reorder(
    ordered: list[Entry],
    scores: dict[str, float],
    similarity: Mapping[tuple[str, str], float],
    lam: float,
    version_groups: Sequence[Sequence[str]] = (),
) -> list[Entry]:
    """MMR re-order of ``apply_supersession_invariant``'s output (ADR 0078): order only, nothing
    dropped or re-scored.

    A unit is a supersession chain joined with its version group's chains (ADR 0074); its relevance
    is its best score, so ``lam == 1`` reproduces ``ordered``. Each unit's members go back into the
    unit's slots in their old order, so the supersession invariant holds (SPEC §6.3).
    """
    heads = chain_heads(ordered)
    unit_of = dict(heads)
    for group in version_groups:
        group_heads = list(dict.fromkeys(heads[i] for i in group if i in heads))
        for h in group_heads[1:]:
            for k, v in unit_of.items():
                if v == h:
                    unit_of[k] = group_heads[0]
    best: dict[str, float] = {}
    for entry in ordered:
        unit = unit_of[entry.id]
        best[unit] = max(best.get(unit, scores[entry.id]), scores[entry.id])
    relevance = {e.id: best[unit_of[e.id]] for e in ordered}
    by_id = {e.id: e for e in ordered}
    picked = [by_id[i] for i in mmr_order([e.id for e in ordered], relevance, similarity, lam)]
    slots: dict[str, list[int]] = {}
    for position, entry in enumerate(picked):
        slots.setdefault(unit_of[entry.id], []).append(position)
    members: dict[str, list[Entry]] = {}
    for entry in ordered:
        members.setdefault(unit_of[entry.id], []).append(entry)
    result = list(picked)
    for unit, positions in slots.items():
        for position, member in zip(positions, members[unit], strict=True):
            result[position] = member
    return result


MAX_GROUPED_CANDIDATES = 40
MAX_GROUP_SIZE = 3  # a pair or a triple, never a cluster


def _group_key(entry: Entry) -> str | None:
    """Grouping key (ADR 0074): one fleet, or org-scoped; ``None`` groups with nobody."""
    if entry.scope == SCOPE_ORG:
        return "org"
    return f"fleet:{entry.fleet_id}" if entry.fleet_id else None


def group_versions(
    ranked: list[Entry],
    similarity: Mapping[tuple[str, str], float],
    threshold: float,
    feedback: Mapping[str, FeedbackSignals],
) -> list[list[Entry]]:
    """Group unlinked near-copies among ``ranked`` (active, best first), ADR 0074.

    The best ungrouped candidate anchors a group; later ones join at similarity >= ``threshold``
    within ``MAX_GROUP_SIZE`` and one ``_group_key``. Groups of two or more are returned, newest
    first by the later of ``occurred_at`` and ``last_confirmed_at``, flagged members (stale, wrong,
    contested) never before an unflagged one. Pure: no score changes, nothing hidden.
    """

    def newest(entry: Entry) -> datetime:
        confirmed = feedback.get(entry.id, NO_SIGNALS).last_confirmed_at
        return max(entry.occurred_at, confirmed) if confirmed else entry.occurred_at

    def flagged(entry: Entry) -> bool:
        _, stale, wrong = feedback.get(entry.id, NO_SIGNALS).counts
        return stale + wrong > 0

    taken: set[str] = set()
    groups: list[list[Entry]] = []
    for position, anchor in enumerate(ranked):
        key = _group_key(anchor)
        if anchor.id in taken or key is None:
            continue
        members = [anchor]
        for other in ranked[position + 1 :]:
            if len(members) >= MAX_GROUP_SIZE:
                break
            if other.id in taken or _group_key(other) != key:
                continue
            pair = (anchor.id, other.id) if anchor.id < other.id else (other.id, anchor.id)
            if similarity.get(pair, -1.0) >= threshold:
                members.append(other)
        if len(members) < 2:
            continue
        taken.update(m.id for m in members)
        members.sort(key=newest, reverse=True)
        members.sort(key=flagged)  # stable: unflagged first, newest first within each
        groups.append(members)
    return groups


def _similar_map(groups: list[list[Entry]]) -> dict[str, tuple[SimilarEntry, ...]]:
    """Each grouped entry's other group members, newest first by ``occurred_at``."""
    out: dict[str, tuple[SimilarEntry, ...]] = {}
    for group in groups:
        for entry in group:
            others = sorted(
                (m for m in group if m.id != entry.id), key=lambda m: m.occurred_at, reverse=True
            )
            out[entry.id] = tuple(SimilarEntry(m.id, m.occurred_at) for m in others)
    return out


def apply_supersession_invariant(
    entries: list[Entry],
    scores: dict[str, float],
    *,
    version_groups: Sequence[Sequence[str]] = (),
) -> list[Entry]:
    """Within a supersession chain the newest entry outranks its predecessors (SPEC.md §6.3).

    Groups by chain head, best score first, newest ``created_at`` first within a group; ties keep
    the order of ``entries`` (stable sorts). ``version_groups`` (ADR 0074; ids in display order)
    place their chains together at the best member's position. Every entry keeps a slot.
    """
    head_of = chain_heads(entries)
    groups: dict[str, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(head_of[entry.id], []).append(entry)
    for members in groups.values():
        members.sort(key=lambda e: e.created_at, reverse=True)

    # Chains joined by a version group, keyed by the first-seen head.
    merged: dict[str, list[str]] = {}
    merged_into: dict[str, str] = {}
    for version_group in version_groups:
        group_heads = [
            h
            for h in dict.fromkeys(head_of[i] for i in version_group if i in head_of)
            if h not in merged_into
        ]
        if len(group_heads) < 2:
            continue
        # Present at the earliest position any of its chains had.
        first = next(h for h in groups if h in group_heads)
        merged[first] = group_heads
        merged_into.update(dict.fromkeys(group_heads, first))

    scored_groups: list[_ScoredGroup] = []
    for head, members in groups.items():
        if head in merged_into and merged_into[head] != head:
            continue  # emitted with the group's first chain
        if head in merged:
            members = [m for h in merged[head] for m in groups[h]]
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
