"""Search service — the retrieval pipeline (SPEC.md §6).

Keyword + vector streams, RRF fusion, decay-aware rescoring and the
supersession ranking invariant. Orchestration only: the math lives in
``hivemind.retrieval``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from hivemind.config import SearchConfig
from hivemind.domain.access import Visibility
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


@dataclass(frozen=True, slots=True)
class SimilarEntry:
    """Another member of a hit's version group (ADR 0074): an id the caller
    can already read, and when its memory happened."""

    entry_id: str
    occurred_at: datetime


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
    # When someone other than the author last used the entry and said what
    # they checked, with no flag since (SPEC §4.2); ranked on only when
    # ``recency_from_confirmation`` is on (ADR 0071).
    last_confirmed_at: datetime | None = None
    # The evidence on the hit (SPEC §6.1), display only: readers disagree
    # (a helpful and a stale or wrong report), the newest stale or wrong
    # report with a note when no confirmation is newer, and how many entries
    # the hit replaced that the caller may read. ``state`` only under
    # ``include_inactive``.
    contested: bool = False
    correction: Correction | None = None
    supersedes: int = 0
    state: EntryState | None = None
    # Unlinked near-copies of this entry among the same search's candidates,
    # newest first (ADR 0074); empty unless version grouping is on and found
    # some. Display only.
    similar: tuple[SimilarEntry, ...] = ()
    # Entries this insight or decision links to (see_also) that were
    # withdrawn, superseded by someone else or flagged since the link was
    # made, at most 3 (ADR 0081). Display only.
    upstream_changed: tuple[UpstreamChange, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchResult:
    """A page of hits; ``degraded`` when the embedder failed and the page
    is keyword-only (ADR 0048)."""

    hits: list[Hit]
    degraded: bool = False
    # The hint the scores were computed under (ADR 0073): ``DEFAULT`` unless
    # the server setting is on and the caller asked for another.
    recency: RecencyHint = RecencyHint.DEFAULT


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
        """Run a hybrid query and return compact hits, best first.

        If the embedder fails the result is keyword-only and ``degraded``
        (ADR 0048), not an error. A caller's first-page search is counted
        per home fleet (ADR 0056). ``recency`` is a per-call hint (ADR 0073):
        it changes the scoring only while ``recency_hint_enabled`` is on, and
        the result says which hint was applied. Send the same hint on every
        page of one search. ``explore`` re-orders the ranked candidates for
        diversity (ADR 0078) and is counted too; off, the ranking is
        untouched. ``count=False`` leaves the search out of the counters
        (the admin search, ADR 0080).
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
        """The search itself (``search_result`` without the counting)."""
        # Validate before any embedder or store call (ADR 0040).
        check_query(query)
        check_pagination(limit, offset)
        filters = filters or EntryFilters()
        filters.validate()
        limit = limit if limit is not None else self._config.default_limit
        offset = offset or 0
        top_k = self._config.candidate_top_k
        applied = self._config.applied_hint(recency)

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
            return SearchResult([], degraded, applied)

        # 3. Fetch candidates + batched feedback signals.
        candidate_ids = list(fused)
        entries, signals = await _gather(
            self._store.get_entries(candidate_ids),
            self._store.feedback_signals(
                candidate_ids, count_supersedes=True, visibility=visibility
            ),
        )
        quality_kwargs = self._config.quality_kwargs()

        # 4. Decay-aware rescore (SPEC.md §6.4).
        now = self._now_fn()
        scored: list[Entry] = []
        entry_scores: dict[str, float] = {}
        entry_feedback: dict[str, FeedbackSignals] = {}
        # Fused-rank order, not row order: ties are common and the stable
        # sorts below keep this order (SP-5).
        for entry in (entries[eid] for eid in candidate_ids if eid in entries):
            if not filters.include_inactive and entry.state is not EntryState.ACTIVE:
                continue
            entry_signals = signals.get(entry.id, NO_SIGNALS)
            entry_feedback[entry.id] = entry_signals
            helpful, stale, wrong = entry_signals.counts
            quality = feedback_quality(helpful, stale, wrong, **quality_kwargs)
            terms = self._config.recency_terms(entry.kind, recency)
            score = entry_score(
                fused[entry.id],
                entry.importance,
                entry.occurred_at,
                quality,
                now,
                terms.half_life_days,
                recency_floor=terms.recency_floor,
                social_max=self._config.social_max,
                confirmed_at=(entry_signals.last_confirmed_at if terms.use_confirmation else None),
            )
            entry_scores[entry.id] = score
            scored.append(entry)

        # 5. Version groups (ADR 0074, off unless configured), then the
        # supersession ranking invariant (SPEC.md §6.3). Run after the two
        # concurrent reads above, never beside them (ADR 0059).
        version_groups: list[list[Entry]] = []
        threshold = self._config.version_group_min_similarity
        # With grouping and explore both on, explore's candidates (every
        # scored entry) are a superset of the grouped ones: read the
        # similarities once. A failed shared read leaves each step to read
        # (and fail) on its own, as when only one is on.
        shared: dict[tuple[str, str], float] | None = None
        if threshold is not None and explore and len(scored) > 1:
            try:
                shared = await self._store.pairwise_similarity([e.id for e in scored])
            except Exception:
                logger.warning("shared similarity read failed", exc_info=True)
        if threshold is not None:
            version_groups = await self._version_groups(
                scored, entry_scores, entry_feedback, threshold, shared
            )
        ordered = apply_supersession_invariant(
            scored, entry_scores, version_groups=[[m.id for m in g] for g in version_groups]
        )
        similar = _similar_map(version_groups)

        # 5b. Opt-in diversity re-order (ADR 0078); never on the default path.
        if explore and len(ordered) > 1:
            ordered = await self._explore_order(
                ordered, entry_scores, [[m.id for m in g] for g in version_groups], shared
            )

        # 6. Pagination (SPEC.md §5.3: limit/offset on search and list).
        page = ordered[offset : offset + limit]
        upstream = await self._upstream_changes(page, visibility)
        hits = [
            self._to_hit(
                entry,
                entry_scores[entry.id],
                entry_feedback[entry.id],
                with_state=filters.include_inactive,
                similar=similar.get(entry.id, ()),
                upstream_changed=upstream.get(entry.id, ()),
            )
            for entry in page
        ]
        return SearchResult(hits, degraded, applied)

    async def _upstream_changes(
        self, page: list[Entry], visibility: Visibility | None
    ) -> dict[str, tuple[UpstreamChange, ...]]:
        """The ``upstream_changed`` marker of each insight or decision on
        the page (ADR 0081): one batched read after the gathered ones, only
        for the page. Best-effort like version grouping: a display nicety,
        so a failed read means no marker, never a failed search."""
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
        """The version groups among the best candidates (ADR 0074), each
        ordered for display. Best-effort: grouping is a display nicety, so a
        failed read means no groups, never a failed search. ``similarity``, when
        given, covers at least the candidates and spares the read."""
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
        """``ordered`` re-ordered by MMR over the stored vectors (ADR 0078).

        Runs after the gathered reads (ADR 0059), so a search never has more
        than two store calls in flight. Best-effort: a failed similarity read
        keeps the normal order. ``similarity``, when given, covers ``ordered``
        and spares the read.
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
        """The vector stream, or None on an ``EmbeddingError`` (ADR 0048);
        store failures propagate."""
        try:
            query_vector = await self._embedder.embed_text(query)
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
    """The chain head of each candidate id: follow ``superseded_by`` until
    the link leaves the candidate set."""
    # The candidates by id (the first one, should an id repeat).
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
    """Re-order the output of ``apply_supersession_invariant`` by MMR for
    diversity (ADR 0078). Only the order changes: nothing is dropped or
    re-scored.

    An entry's unit is its supersession chain, joined with the chains of its
    version group (ADR 0074) when it has one. Relevance is the best score in
    the unit (the key the invariant ranks units by), so ``lam == 1``
    reproduces ``ordered``. After MMR each unit's members are put back, in
    the order ``ordered`` had them (chains newest first, a version group in
    its display order), into the slots the unit holds, so the supersession
    invariant still holds (SPEC §6.3). Members of one version group are
    not kept adjacent: spreading near-copies is what MMR is for.
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


# Version grouping looks at no more than the two streams' default candidates.
MAX_GROUPED_CANDIDATES = 40
# A group of unlinked versions is a pair or a triple, never a cluster.
MAX_GROUP_SIZE = 3


def _group_key(entry: Entry) -> str | None:
    """Who may be grouped with whom (ADR 0074): entries of one fleet, or
    org-scoped entries. ``None``: with nobody."""
    if entry.scope == "org":
        return "org"
    return f"fleet:{entry.fleet_id}" if entry.fleet_id else None


def group_versions(
    ranked: list[Entry],
    similarity: Mapping[tuple[str, str], float],
    threshold: float,
    feedback: Mapping[str, FeedbackSignals],
) -> list[list[Entry]]:
    """Group unlinked near-copies among ``ranked`` (active candidates, best
    score first), ADR 0074.

    Walking down the ranking, the best ungrouped candidate is a group's
    anchor; a later ungrouped candidate joins when its similarity to the
    anchor is at least ``threshold``, the group has fewer than
    ``MAX_GROUP_SIZE`` members and both belong to one fleet or are both
    org-scoped. Only groups of two or more are returned, each ordered for
    display: newest first by the later of ``occurred_at`` and
    ``last_confirmed_at``, except that a member with a stale or wrong
    report (contested included) never precedes an unflagged one. Pure: it
    changes no score and hides nothing.
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
    """For each grouped entry, its group's other members, newest first by
    ``occurred_at``."""
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
    """Enforce: within a supersession chain, the newest entry outranks all
    predecessors (SPEC.md §6.3).

    Group by chain head, order groups by their best score, and within a
    group newest ``created_at`` first. Ties keep the order of ``entries``
    (stable sorts; SP-5).

    ``version_groups`` (ADR 0074; ids, already in display order) add edges
    between chains: the chains of one group are placed together, in the
    group's order, at the position of the best-scoring member, each chain
    keeping its own order. Every entry keeps a slot and no score changes.
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
