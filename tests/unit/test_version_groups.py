"""Read-time grouping of unlinked versions of one fact (ADR 0074).

Hand-built vectors (the query is the first axis), so what is similar to
what is exact. Off (``None``) is today's search, bit for bit; on, grouping
only moves members next to each other: no score changes, nothing is hidden.
"""

from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from hivemind.api.schemas import HitOut
from hivemind.config import SearchConfig, Settings
from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import Entry, EntryDraft, EntryFilters, Kind
from hivemind.domain.feedback import Feedback, FeedbackSignals, Verdict
from hivemind.embeddings import EmbeddingError
from hivemind.memstore import MemoryStore
from hivemind.services.search import (
    Hit,
    SearchService,
    SimilarEntry,
    apply_supersession_invariant,
    group_versions,
)
from hivemind.wire import hit_dict
from tests.fakes import FIXED_NOW, make_clock, make_search_config

TAU = 0.9
DIM = 4
QUERY = "pool size"


class AxisEmbedder:
    """The query is axis 0; entries get their vectors from the test."""

    model_name = "axis"
    dimension = DIM

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    async def embed_text(self, text: str) -> list[float]:
        if self.fail:
            raise EmbeddingError("down")
        return [1.0, 0.0, 0.0, 0.0]

    async def embed_entry(self, draft: EntryDraft) -> list[float]:  # pragma: no cover
        raise AssertionError("tests store their own vectors")


def vec(angle: float, third: float = 0.0) -> list[float]:
    """A vector at ``angle`` (units of 0.1 rad) from the query on the first
    two axes; same angle = same direction."""
    import math

    a = angle * 0.1
    return [math.cos(a), math.sin(a), third, 0.0]


def day(n: float) -> datetime:
    return FIXED_NOW - timedelta(days=n)


async def put(
    store: MemoryStore,
    summary: str,
    embedding: list[float] | None,
    *,
    age: float = 1.0,
    scope: str = "org",
    fleet_id: str | None = None,
    importance: int = 3,
    author: str = "alice",
) -> Entry:
    draft = EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author=author,
        agent="a",
        occurred_at=day(age),
        scope=scope,
        fleet_id=fleet_id,
        importance=importance,
        importance_source="caller" if importance != 3 else "default",  # type: ignore[arg-type]
    )
    return await store.create_entry(draft, embedding, "axis")


def service(
    store: MemoryStore, tau: float | None = TAU, embedder: AxisEmbedder | None = None, **kw: object
) -> SearchService:
    config = replace(
        make_search_config(candidate_top_k=20, default_limit=20),
        version_group_min_similarity=tau,
        **kw,  # type: ignore[arg-type]
    )
    return SearchService(store, embedder or AxisEmbedder(), config, now_fn=lambda: FIXED_NOW)


async def ids(svc: SearchService, **kw: object) -> list[str]:
    return [h.entry_id for h in await svc.search(QUERY, **kw)]  # type: ignore[arg-type]


# --- off is today's search ----------------------------------------------------------


class _CountingStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(make_clock())
        self.pairwise_calls = 0
        self.inflight = 0
        self.max_inflight = 0
        self.seen_inflight_at_pairwise = -1
        self.fail = False

    async def _track[T](self, work: object) -> T:
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            import asyncio

            await asyncio.sleep(0)  # let a sibling start, if one is running
            return await work  # type: ignore[no-any-return,misc]
        finally:
            self.inflight -= 1

    async def get_entries(self, entry_ids: list[str]) -> dict[str, Entry]:
        return await self._track(super().get_entries(entry_ids))

    async def feedback_signals(self, *a: object, **k: object) -> dict[str, FeedbackSignals]:
        return await self._track(super().feedback_signals(*a, **k))  # type: ignore[arg-type]

    async def pairwise_similarity(self, entry_ids: list[str]) -> dict[tuple[str, str], float]:
        self.pairwise_calls += 1
        self.seen_inflight_at_pairwise = self.inflight
        if self.fail:
            raise RuntimeError("db down")
        return await super().pairwise_similarity(entry_ids)


async def _pool(store: MemoryStore) -> dict[str, Entry]:
    old = await put(store, "pool size is 20", vec(1), age=30)
    new = await put(store, "pool size raised to 90", vec(1.1), age=2)
    other = await put(store, "pool timeout is 5s", vec(1, third=2.0), age=3)
    far = await put(store, "pool of workers", vec(8), age=1, importance=4)
    return {"old": old, "new": new, "other": other, "far": far}


async def test_off_never_reads_vectors_and_equals_a_threshold_that_groups_nothing() -> None:
    store = _CountingStore()
    await _pool(store)
    off = await service(store, None).search(QUERY)
    assert store.pairwise_calls == 0
    assert all(not h.similar for h in off)
    # τ so high no pair reaches it: the same hits, same order, same scores.
    unreachable = await service(store, 0.9999999).search(QUERY)
    assert [(h.entry_id, h.score) for h in unreachable] == [(h.entry_id, h.score) for h in off]
    assert store.pairwise_calls == 1
    assert SearchConfig().version_group_min_similarity is None


async def test_off_is_the_old_invariant_when_no_groups_are_given() -> None:
    store = MemoryStore(make_clock())
    pool = await _pool(store)
    entries = list(pool.values())
    scores = {e.id: s for e, s in zip(entries, (0.4, 0.9, 0.5, 0.5), strict=True)}
    assert apply_supersession_invariant(entries, scores) == apply_supersession_invariant(
        entries, scores, version_groups=()
    )


# --- grouping -----------------------------------------------------------------------


async def test_two_versions_sit_together_newest_first_each_naming_the_other() -> None:
    store = MemoryStore(make_clock())
    pool = await _pool(store)
    off = await service(store, None).search(QUERY)
    on = await service(store).search(QUERY)

    order = [h.entry_id for h in on]
    old, new = pool["old"].id, pool["new"].id
    assert abs(order.index(old) - order.index(new)) == 1
    assert order.index(new) < order.index(old)  # newest first
    by_id = {h.entry_id: h for h in on}
    assert by_id[old].similar == (SimilarEntry(new, pool["new"].occurred_at),)
    assert by_id[new].similar == (SimilarEntry(old, pool["old"].occurred_at),)
    assert not by_id[pool["other"].id].similar
    # Nothing hidden, no score changed.
    assert sorted(order) == sorted(h.entry_id for h in off)
    assert {h.entry_id: h.score for h in on} == {h.entry_id: h.score for h in off}


async def test_the_group_takes_its_best_members_position() -> None:
    store = MemoryStore(make_clock())
    pool = await _pool(store)
    on = await service(store).search(QUERY)
    scores = {h.entry_id: h.score for h in on}
    first_of_group = min(
        (pool["old"].id, pool["new"].id), key=lambda i: [h.entry_id for h in on].index(i)
    )
    assert scores[first_of_group] == max(scores[pool["old"].id], scores[pool["new"].id])
    # Not above the group's best position: everything ahead of the group
    # scores at least as high as the group's best.
    ahead = on[: [h.entry_id for h in on].index(first_of_group)]
    assert all(h.score >= scores[first_of_group] for h in ahead)


async def test_a_group_has_at_most_three_members_and_joins_by_the_anchor_only() -> None:
    store = MemoryStore(make_clock())
    same = [await put(store, f"pool size v{i}", vec(1), age=10 - i) for i in range(4)]
    on = await service(store).search(QUERY)
    sizes = sorted(len(h.similar) + 1 if h.similar else 1 for h in on)
    assert sizes == [1, 3, 3, 3]
    assert {h.entry_id for h in on} == {e.id for e in same}

    # A chain a~b~c with a !~ c: c is not grouped through b.
    store = MemoryStore(make_clock())
    a = await put(store, "pool size a", vec(0), age=3, importance=5)
    b = await put(store, "pool size b", vec(1.4), age=2)
    c = await put(store, "pool size c", vec(2.8), age=1)
    # cos(0.14)=0.990, cos(0.28)=0.961: a~b and b~c at 0.97, a~c at 0.92; at τ 0.98 only a~b groups
    on = await service(store, 0.98).search(QUERY)
    by_id = {h.entry_id: h for h in on}
    assert [m.entry_id for m in by_id[a.id].similar] == [b.id]
    assert not by_id[c.id].similar


async def test_fleet_rule_same_fleet_or_both_org() -> None:
    store = MemoryStore(make_clock())
    f1a = await put(store, "pool size a", vec(1), scope="fleet", fleet_id="f1", age=5)
    f1b = await put(store, "pool size b", vec(1), scope="fleet", fleet_id="f1", age=1)
    f2 = await put(store, "pool size c", vec(1), scope="fleet", fleet_id="f2", age=2)
    org1 = await put(store, "pool size d", vec(1), age=3)
    org2 = await put(store, "pool size e", vec(1), age=4)
    on = {h.entry_id: h for h in await service(store).search(QUERY)}
    assert [m.entry_id for m in on[f1a.id].similar] == [f1b.id]
    assert [m.entry_id for m in on[org1.id].similar] == [org2.id]
    assert not on[f2.id].similar


async def test_a_flagged_newer_member_does_not_jump_ahead_of_an_unflagged_older_one() -> None:
    store = MemoryStore(make_clock())
    old = await put(store, "pool size is 20", vec(1), age=30)
    new = await put(store, "pool size raised to 90", vec(1), age=2)
    first = [h.entry_id for h in await service(store).search(QUERY)]
    assert first.index(new.id) < first.index(old.id)

    await store.record_feedback(Feedback(new.id, "bob", "bob", Verdict.STALE, None, FIXED_NOW))
    flagged = [h.entry_id for h in await service(store).search(QUERY)]
    assert flagged.index(old.id) < flagged.index(new.id)
    # Both still shown, still naming each other.
    hits = {h.entry_id: h for h in await service(store).search(QUERY)}
    assert hits[old.id].similar and hits[new.id].similar


def _signals(confirmed: datetime | None = None, stale: int = 0) -> FeedbackSignals:
    return FeedbackSignals(0, stale, 0, confirmed, None)


def _entry(i: str, age: float, scope: str = "org", fleet: str | None = None) -> Entry:
    return Entry(
        id=i,
        kind=Kind.FACT,
        summary=i,
        author="a",
        agent="a",
        occurred_at=day(age),
        created_at=day(age),
        scope=scope,
        fleet_id=fleet,
    )


def test_a_confirmation_counts_as_recency_in_the_group_order() -> None:
    old, new = _entry("a", 30), _entry("b", 2)
    sims = {("a", "b"): 0.99}
    plain = group_versions([old, new], sims, TAU, {})
    assert [e.id for e in plain[0]] == ["b", "a"]
    confirmed = group_versions([old, new], sims, TAU, {"a": _signals(confirmed=day(0.5))})
    assert [e.id for e in confirmed[0]] == ["a", "b"]


def test_missing_pairs_and_unscoped_entries_never_group() -> None:
    a, b = _entry("a", 3), _entry("b", 2)
    assert group_versions([a, b], {}, TAU, {}) == []  # no stored vector for one
    assert group_versions([a, b], {("a", "b"): 0.8999}, TAU, {}) == []
    assert len(group_versions([a, b], {("a", "b"): 0.9}, TAU, {})) == 1  # at τ joins
    lone = _entry("x", 1, scope="fleet", fleet=None)
    assert group_versions([lone, _entry("y", 1, scope="fleet")], {("x", "y"): 1.0}, TAU, {}) == []


def test_version_groups_join_whole_chains_and_keep_each_chain_in_order() -> None:
    head_a, old_a = _entry("a", 1), _entry("a0", 9)
    old_a = replace(old_a, superseded_by="a", created_at=day(9))
    head_a = replace(head_a, created_at=day(1))
    other = replace(_entry("b", 2), created_at=day(2))
    filler = _entry("z", 1)
    entries = [head_a, old_a, filler, other]
    scores = {"a": 0.9, "a0": 0.2, "z": 0.8, "b": 0.5}
    base = [e.id for e in apply_supersession_invariant(entries, scores)]
    assert base == ["a", "a0", "z", "b"]
    grouped = [
        e.id for e in apply_supersession_invariant(entries, scores, version_groups=[["b", "a"]])
    ]
    # Group order b, a; the chain a/a0 stays together and ordered; z last.
    assert grouped == ["b", "a", "a0", "z"]
    assert sorted(grouped) == sorted(base)
    assert grouped.index("a") < grouped.index("a0")


# --- invariants: nothing hidden, nothing above the best, scores untouched -------------


@pytest.mark.parametrize("seed", range(12))
async def test_grouping_never_hides_an_entry_and_never_lifts_a_member_over_the_best(
    seed: int,
) -> None:
    rng = random.Random(seed)
    store = MemoryStore(make_clock())
    for i in range(rng.randint(6, 24)):
        await put(
            store,
            f"pool size entry {i}",
            vec(rng.choice([0, 1, 1.2, 2, 3, 5, 8]), third=rng.choice([0.0, 0.2])),
            age=rng.uniform(0.1, 60),
            importance=rng.choice([1, 3, 3, 5]),
            scope=rng.choice(["org", "org", "fleet"]),
            fleet_id=rng.choice([None, "f1", "f2"]) if rng.random() < 0.5 else None,
        )
    tau = rng.choice([0.8, 0.9, 0.97])
    off = await service(store, None).search(QUERY, limit=40)
    on = await service(store, tau).search(QUERY, limit=40)

    assert sorted(h.entry_id for h in on) == sorted(h.entry_id for h in off)  # nothing hidden
    assert {h.entry_id: h.score for h in on} == {h.entry_id: h.score for h in off}
    position = {h.entry_id: i for i, h in enumerate(on)}
    score = {h.entry_id: h.score for h in on}

    def key(hit: Hit) -> float:  # the best score of the hit's group (itself if alone)
        return max([hit.score, *(score[m.entry_id] for m in hit.similar)])

    # A group sits at its best member's position: the page's group keys never
    # rise, so no member is lifted over anything that outscores the group's best.
    keys = [key(h) for h in on]
    assert keys == sorted(keys, reverse=True)
    for hit in on:
        if not hit.similar:
            continue
        members = [hit.entry_id, *(m.entry_id for m in hit.similar)]
        assert 2 <= len(members) <= 3
        # Adjacent: the group occupies consecutive slots.
        slots = sorted(position[m] for m in members)
        assert slots == list(range(slots[0], slots[0] + len(members)))
        # Newest first in `similar`.
        dates = [m.occurred_at for m in hit.similar]
        assert dates == sorted(dates, reverse=True)
        # Symmetry: each member names the others.
        for m in hit.similar:
            back = {x.entry_id for x in next(h for h in on if h.entry_id == m.entry_id).similar}
            assert hit.entry_id in back


# --- store use, degraded mode, missing vectors, visibility ------------------------------


async def test_the_pairwise_read_runs_after_the_two_concurrent_reads_never_beside_them() -> None:
    store = _CountingStore()
    await _pool(store)
    await service(store).search(QUERY)
    assert store.pairwise_calls == 1
    assert store.seen_inflight_at_pairwise == 0  # ADR 0059: no third concurrent read
    assert store.max_inflight <= 2


async def test_a_failing_pairwise_read_gives_the_ungrouped_page_not_an_error() -> None:
    store = _CountingStore()
    await _pool(store)
    store.fail = True
    hits = await service(store).search(QUERY)
    assert [h.entry_id for h in hits] == [
        h.entry_id for h in await service(store, None).search(QUERY)
    ]
    assert all(not h.similar for h in hits)


async def test_grouping_works_when_the_embedder_is_down() -> None:
    store = MemoryStore(make_clock())
    pool = await _pool(store)
    result = await service(store, embedder=AxisEmbedder(fail=True)).search_result(QUERY)
    assert result.degraded
    by_id = {h.entry_id: h for h in result.hits}
    assert by_id[pool["old"].id].similar and by_id[pool["new"].id].similar


async def test_entries_without_a_stored_vector_are_shown_ungrouped() -> None:
    store = MemoryStore(make_clock())
    old = await put(store, "pool size is 20", vec(1), age=30)
    bare = await put(store, "pool size raised to 90", None, age=2)
    result = await service(store, embedder=AxisEmbedder(fail=True)).search_result(QUERY)
    assert {h.entry_id for h in result.hits} == {old.id, bare.id}
    assert all(not h.similar for h in result.hits)


async def test_inactive_entries_are_not_grouped() -> None:
    store = MemoryStore(make_clock())
    old = await put(store, "pool size is 20", vec(1), age=30)
    new = await put(store, "pool size raised to 90", vec(1), age=2)
    await store.withdraw_entry(old.id, "wrong", "alice")
    hits = await service(store).search(QUERY, EntryFilters(include_inactive=True))
    assert {h.entry_id for h in hits} == {old.id, new.id}
    assert all(not h.similar for h in hits)


async def test_similar_lists_only_what_the_caller_can_already_see() -> None:
    store = MemoryStore(make_clock())
    mine_a = await put(store, "pool size a", vec(1), scope="fleet", fleet_id="f1", age=5)
    mine_b = await put(store, "pool size b", vec(1), scope="fleet", fleet_id="f1", age=1)
    theirs = await put(store, "pool size c", vec(1), scope="fleet", fleet_id="f2", age=2)
    reader = Visibility(level=TrustLevel.CONTRIBUTOR, name="r", home_fleet_id="f1")
    hits = await service(store).search(QUERY, visibility=reader)
    named = {m.entry_id for h in hits for m in h.similar} | {h.entry_id for h in hits}
    assert theirs.id not in named
    assert named == {mine_a.id, mine_b.id}


# --- surfaces -----------------------------------------------------------------------


def _hit(similar: tuple[SimilarEntry, ...]) -> Hit:
    return Hit(
        entry_id="e1",
        kind=Kind.FACT,
        summary="s",
        tags=(),
        author="a",
        agent="a",
        occurred_at=day(1),
        score=0.5,
        similar=similar,
    )


def test_similar_is_omitted_on_mcp_and_null_on_rest_when_empty() -> None:
    plain = _hit(())
    assert "similar" not in hit_dict(plain)
    assert HitOut.from_hit(plain).similar is None

    grouped = _hit((SimilarEntry("e2", day(3)), SimilarEntry("e3", day(9))))
    assert hit_dict(grouped)["similar"] == [
        {"id": "e2", "occurred_at": day(3).isoformat()},
        {"id": "e3", "occurred_at": day(9).isoformat()},
    ]
    rest = HitOut.from_hit(grouped).model_dump(mode="json")["similar"]
    assert [m["id"] for m in rest] == ["e2", "e3"]


# --- config -------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0.0, 1.0, -0.1, 1.5, float("nan")])
def test_the_cutoff_must_be_inside_zero_and_one(bad: float) -> None:
    with pytest.raises(ValueError, match="version_group_min_similarity"):
        SearchConfig(version_group_min_similarity=bad)


@pytest.mark.parametrize(
    ("raw", "expected"), [("", None), ("none", None), ("NONE", None), ("0.85", 0.85)]
)
def test_the_env_spellings(
    raw: str, expected: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIVEMIND_VERSION_GROUP_MIN_SIMILARITY", raw)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.search_config().version_group_min_similarity == expected


def test_off_by_default_in_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIVEMIND_VERSION_GROUP_MIN_SIMILARITY", raising=False)
    assert Settings(_env_file=None).search_config().version_group_min_similarity is None  # type: ignore[call-arg]


async def test_grouping_and_explore_share_one_similarity_read() -> None:
    store = _CountingStore()
    await _pool(store)
    svc = service(store)
    got = await svc.search(QUERY, explore=True)
    assert store.pairwise_calls == 1
    assert any(h.similar for h in got)  # the pool really has a version group

    # The reference: the shared read fails once, so each step reads for
    # itself, the way grouping and explore did before they shared one read.
    store.pairwise_calls = 0
    real = MemoryStore.pairwise_similarity

    async def fail_once(entry_ids: list[str]) -> dict[tuple[str, str], float]:
        store.pairwise_calls += 1
        if store.pairwise_calls == 1:
            raise RuntimeError("db down")
        return await real(store, entry_ids)

    store.pairwise_similarity = fail_once  # type: ignore[method-assign]
    expected = await svc.search(QUERY, explore=True)
    assert store.pairwise_calls == 3  # the failed shared read, then one per step
    assert got == expected
