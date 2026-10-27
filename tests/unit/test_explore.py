"""Opt-in explore search (ADR 0078): MMR re-ordering over stored vectors,
its plumbing through REST and MCP, and its counter."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import asdict

import pytest

from hivemind.config import SearchConfig
from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import Entry, EntryDraft, EntryFilters
from hivemind.mcp.app import McpHivemind, hive_search, hive_write
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.retrieval.mmr import mmr_order
from hivemind.services.metrics import MetricsService
from hivemind.services.search import SearchService
from tests.fakes import make_clock, make_embedder
from tests.unit.test_api_endpoints import make_client, make_hivemind_app, post_entry
from tests.unit.test_mcp_app import ALICE, build_app
from tests.unit.test_prometheus import samples

# --------------------------------------------------------------------------- #
# the pure MMR order
# --------------------------------------------------------------------------- #


def test_mmr_sinks_a_near_copy_of_a_higher_pick() -> None:
    relevance = {"a": 1.0, "b": 0.9, "c": 0.8}
    similarity = {("a", "b"): 0.99, ("a", "c"): 0.1, ("b", "c"): 0.1}
    assert mmr_order(["a", "b", "c"], relevance, similarity, 0.7) == ["a", "c", "b"]


def test_mmr_lambda_one_is_relevance_order_and_keeps_ties_stable() -> None:
    relevance = {"a": 1.0, "b": 1.0, "c": 0.5, "d": 1.0}
    similarity = {("a", "b"): 1.0, ("a", "d"): 1.0, ("b", "d"): 1.0}
    assert mmr_order(["a", "b", "c", "d"], relevance, similarity, 1.0) == ["a", "b", "d", "c"]


def test_mmr_never_drops_or_repeats_and_missing_pairs_cost_nothing() -> None:
    ids = [f"e{i}" for i in range(7)]
    relevance = {i: 1.0 - n * 0.1 for n, i in enumerate(ids)}
    for lam in (0.05, 0.5, 0.7, 1.0):
        assert sorted(mmr_order(ids, relevance, {}, lam)) == sorted(ids)
        # No similarity known: relevance order.
        assert mmr_order(ids, relevance, {}, lam) == ids
    assert mmr_order([], {}, {}, 0.7) == []


def test_mmr_negative_similarity_is_not_a_bonus() -> None:
    relevance = {"a": 1.0, "b": 0.9}
    assert mmr_order(["a", "b"], relevance, {("a", "b"): -0.8}, 0.5) == ["a", "b"]


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #

READER = Visibility(TrustLevel.CONTRIBUTOR, "reader", home_fleet_id=None)


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


class SimStore(MemoryStore):
    """A MemoryStore whose pairwise similarity a test dictates (by summary)."""

    def __init__(self, clock) -> None:  # type: ignore[no-untyped-def]
        super().__init__(clock)
        self.sims: dict[tuple[str, str], float] = {}
        self.by_summary: dict[str, str] = {}
        self.pairwise_calls = 0
        self.fail = False
        self.inflight = 0
        self.max_inflight = 0

    def set_sim(self, a: str, b: str, value: float) -> None:
        self.sims[_pair(self.by_summary[a], self.by_summary[b])] = value

    async def pairwise_similarity(self, entry_ids: list[str]) -> dict[tuple[str, str], float]:
        self.pairwise_calls += 1
        if self.fail:
            raise RuntimeError("vector read failed")
        return {k: v for k, v in self.sims.items() if k[0] in entry_ids and k[1] in entry_ids}


async def _write(store: SimStore, summary: str, importance: int = 3, **kw: object) -> str:
    embedder = make_embedder()
    source = "caller" if importance != 3 else "default"
    entry = await store.create_entry(
        EntryDraft(
            kind="fact",
            summary=summary,
            author="writer",
            agent="writer",
            importance=importance,
            importance_source=source,  # type: ignore[arg-type]
            **kw,  # type: ignore[arg-type]
        ),
        await embedder.embed_text(summary),
        "test-model",
    )
    store.by_summary[summary] = entry.id
    return entry.id


async def _fixture(lam: float = 0.7) -> tuple[SimStore, SearchService]:
    clock = make_clock()
    store = SimStore(clock)
    # The wide importance factor (ADR 0088 off) keeps the score gaps these
    # fixtures were built around.
    config = SearchConfig(
        candidate_top_k=10, default_limit=10, explore_lambda=lam, narrow_importance=False
    )
    return store, SearchService(store, make_embedder(), config, now_fn=clock)


async def _three_with_a_near_copy() -> tuple[SimStore, SearchService, list[str]]:
    """Three hits; the second of the normal order is a near-copy of the first
    and the third is distant from both. Returns the normal order."""
    store, service = await _fixture()
    for summary, importance in (("alpha one", 5), ("alpha two", 4), ("alpha three", 3)):
        await _write(store, summary, importance=importance)
    normal = [h.entry_id for h in await service.search("alpha", visibility=READER)]
    assert len(normal) == 3
    first, second, third = normal
    store.sims[_pair(first, second)] = 0.99
    store.sims[_pair(first, third)] = 0.1
    store.sims[_pair(second, third)] = 0.1
    return store, service, normal


async def test_explore_lifts_a_different_hit_over_a_near_copy() -> None:
    _, service, normal = await _three_with_a_near_copy()
    explored = await service.search("alpha", visibility=READER, explore=True)
    assert [h.entry_id for h in explored] == [normal[0], normal[2], normal[1]]


async def test_explore_hides_nothing_and_rescores_nothing() -> None:
    _, service, _ = await _three_with_a_near_copy()
    normal = await service.search("alpha", visibility=READER)
    explored = await service.search("alpha", visibility=READER, explore=True)
    assert sorted(h.entry_id for h in explored) == sorted(h.entry_id for h in normal)
    assert {h.entry_id: h.score for h in explored} == {h.entry_id: h.score for h in normal}


async def test_lambda_one_equals_the_normal_order() -> None:
    store, service = await _fixture(lam=1.0)
    for n, imp in enumerate((5, 4, 3, 2)):
        await _write(store, f"alpha n{n}", importance=imp)
    for a in store.by_summary:
        for b in store.by_summary:
            if a < b:
                store.set_sim(a, b, 0.95)
    normal = await service.search("alpha", visibility=READER)
    explored = await service.search("alpha", visibility=READER, explore=True)
    assert [h.entry_id for h in explored] == [h.entry_id for h in normal]


async def test_explore_off_never_reads_vectors_and_equals_the_default_call() -> None:
    store, service, _ = await _three_with_a_near_copy()
    default = await service.search("alpha", visibility=READER)
    off = await service.search("alpha", visibility=READER, explore=False)
    assert off == default
    assert store.pairwise_calls == 0
    await service.search("alpha", visibility=READER, explore=True)
    assert store.pairwise_calls == 1


async def test_explore_keeps_a_supersession_chain_newest_first() -> None:
    clock = make_clock()
    store = SimStore(clock)
    service = SearchService(
        store, make_embedder(), SearchConfig(candidate_top_k=10, explore_lambda=0.4), now_fn=clock
    )
    old = await _write(store, "alpha old", importance=5)
    clock.advance_days(1)
    new = await _write(store, "alpha new", importance=3, supersedes=(old,))
    other = await _write(store, "alpha other", importance=4)
    # The two versions are near-copies; the other one is distant from both.
    store.set_sim("alpha old", "alpha new", 0.99)
    store.set_sim("alpha old", "alpha other", 0.05)
    store.set_sim("alpha new", "alpha other", 0.05)
    hits = await service.search("alpha", visibility=READER, explore=True, filters=_inactive())
    order = [h.entry_id for h in hits]
    assert sorted(order) == sorted([old, new, other])
    assert order.index(new) < order.index(old)


def _inactive() -> EntryFilters:
    return EntryFilters(include_inactive=True)


async def test_explore_page_two_continues_page_one() -> None:
    _, service, _ = await _three_with_a_near_copy()
    first = await service.search("alpha", limit=2, visibility=READER, explore=True)
    second = await service.search("alpha", limit=2, offset=2, visibility=READER, explore=True)
    full = await service.search("alpha", visibility=READER, explore=True)
    assert [h.entry_id for h in first + second] == [h.entry_id for h in full]


async def test_a_failed_similarity_read_keeps_the_normal_order_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, service, _ = await _three_with_a_near_copy()
    normal = await service.search("alpha", visibility=READER)
    store.fail = True
    with caplog.at_level(logging.WARNING):
        got = await service.search("alpha", visibility=READER, explore=True)
    assert got == normal
    record = next(r for r in caplog.records if "explore" in r.getMessage())
    assert record.exc_info is not None


async def test_explore_reads_similarity_only_after_the_concurrent_reads() -> None:
    """ADR 0059: the extra read is sequential, so at most two store calls
    are ever in flight."""
    store, service, _ = await _three_with_a_near_copy()
    active = 0
    peak = 0
    inflight_at_pairwise: list[int] = []

    def wrap(name: str) -> None:
        original = getattr(store, name)

        async def counted(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal active, peak
            if name == "pairwise_similarity":
                inflight_at_pairwise.append(active)
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0)  # let a sibling that runs beside this call start
                return await original(*args, **kwargs)
            finally:
                active -= 1

        setattr(store, name, counted)

    for name in (
        "search_keyword",
        "search_vector",
        "get_entries",
        "feedback_signals",
        "pairwise_similarity",
    ):
        wrap(name)
    await service.search("alpha", visibility=READER, explore=True)
    assert inflight_at_pairwise == [0]  # the similarity read ran alone, once
    assert peak <= 2


async def test_a_single_hit_or_no_hit_skips_the_similarity_read() -> None:
    store, service = await _fixture()
    await _write(store, "alpha lonely")
    await service.search("alpha", visibility=READER, explore=True)
    await service.search("zzyzx", visibility=READER, explore=True)
    assert store.pairwise_calls == 0


def test_explore_lambda_is_validated_in_the_half_open_unit_interval() -> None:
    assert SearchConfig().explore_lambda == 0.7
    assert SearchConfig(explore_lambda=1.0).explore_lambda == 1.0
    for bad in (0.0, -0.1, 1.01, math.nan, math.inf):
        with pytest.raises(ValueError, match="explore_lambda"):
            SearchConfig(explore_lambda=bad)


def test_explore_lambda_env_var_reaches_the_search_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    from hivemind.config import load_settings
    from tests.unit.test_env_var_validation import _clear_hivemind_env

    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    assert load_settings().search_config().explore_lambda == 0.7
    monkeypatch.setenv("HIVEMIND_EXPLORE_LAMBDA", "0.5")
    assert load_settings().search_config().explore_lambda == 0.5
    monkeypatch.setenv("HIVEMIND_EXPLORE_LAMBDA", "0")
    with pytest.raises(ValueError, match="explore_lambda"):
        load_settings()


# --------------------------------------------------------------------------- #
# counting
# --------------------------------------------------------------------------- #


async def test_explore_searches_are_counted_beside_the_search_counts() -> None:
    store, service = await _fixture()
    await _write(store, "alpha lonely")
    reader = Visibility(TrustLevel.CONTRIBUTOR, "reader", home_fleet_id=None)
    await service.search_result("alpha", visibility=reader)
    await service.search_result("alpha", visibility=reader, explore=True)
    await service.search_result("zzyzx", visibility=reader, explore=True)
    await service.search_result("alpha", offset=3, visibility=reader, explore=True)  # not page one
    [count] = await store.search_counts()
    assert (count.searches, count.explore) == (3, 2)


async def test_usage_report_and_prometheus_expose_explore_counts() -> None:
    app = make_hivemind_app()
    fleet = await app.store.create_fleet("data-eng")
    await app.store.record_search(fleet.id, empty=False, explore=True)
    await app.store.record_search(fleet.id, empty=False)
    report = await MetricsService(app.store).usage_report()
    assert report.searches.explore == 1
    assert asdict(report.searches.by_fleet["data-eng"]) == {"total": 2, "empty": 0, "explore": 1}
    client = make_client(app)
    async with client:
        got = samples((await client.get("/metrics")).text)
    assert got[("hivemind_searches_explore_total", (("fleet", "data-eng"),))] == 1
    assert got[("hivemind_searches_total", (("fleet", "data-eng"),))] == 2


# --------------------------------------------------------------------------- #
# REST and MCP plumbing
# --------------------------------------------------------------------------- #


async def test_rest_explore_defaults_off_and_is_accepted_as_a_flag() -> None:
    app = make_hivemind_app()
    client = make_client(app)
    async with client:
        for i in range(3):
            await post_entry(client, "key-alice", f"cohort note {i}")
        headers = {"X-API-Key": "key-bob"}
        plain = await client.post("/v1/search", json={"query": "cohort note"}, headers=headers)
        off = await client.post(
            "/v1/search", json={"query": "cohort note", "explore": False}, headers=headers
        )
        on = await client.post(
            "/v1/search", json={"query": "cohort note", "explore": True}, headers=headers
        )
    assert plain.status_code == off.status_code == on.status_code == 200
    assert [h["entry_id"] for h in plain.json()] == [h["entry_id"] for h in off.json()]
    assert sorted(h["entry_id"] for h in on.json()) == sorted(h["entry_id"] for h in plain.json())
    assert on.json() and set(on.json()[0]) == set(plain.json()[0])


@pytest.mark.parametrize("bad", ["maybe", [1], {"a": 1}, 7])
async def test_rest_explore_must_be_readable_as_a_boolean(bad: object) -> None:
    client = make_client(make_hivemind_app())
    async with client:
        resp = await client.post(
            "/v1/search",
            json={"query": "cohort", "explore": bad},
            headers={"X-API-Key": "key-bob"},
        )
    assert resp.status_code == 422


async def test_rest_explore_search_is_counted_in_the_metrics_report() -> None:
    app = make_hivemind_app()
    client = make_client(app)
    async with client:
        await post_entry(client, "key-alice", "cohort note")
        await client.post(
            "/v1/search",
            json={"query": "cohort", "explore": True},
            headers={"X-API-Key": "key-bob"},
        )
    counts = await app.store.search_counts()
    assert sum(c.explore for c in counts) == 1 and sum(c.searches for c in counts) == 1


@pytest.fixture
def mcp_app() -> McpHivemind:
    clock = make_clock()
    return build_app(MemoryStore(clock), ALICE, clock)


async def test_mcp_explore_is_an_argument_and_defaults_off(mcp_app: McpHivemind) -> None:
    for i in range(3):
        await hive_write(mcp_app, kind="fact", summary=f"cohort note {i}")
    plain = await hive_search(mcp_app, query="cohort note")
    off = await hive_search(mcp_app, query="cohort note", explore=False)
    on = await hive_search(mcp_app, query="cohort note", explore=True)
    assert [h["id"] for h in plain["hits"]] == [h["id"] for h in off["hits"]]
    assert "error" not in on
    assert sorted(h["id"] for h in on["hits"]) == sorted(h["id"] for h in plain["hits"])


async def test_mcp_tool_schema_has_explore_as_an_optional_boolean() -> None:
    server = build_server(build_app(MemoryStore(make_clock()), ALICE, make_clock()))
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["hive_search"].input_schema
    assert schema["properties"]["explore"]["type"] == "boolean"
    assert "explore" not in schema.get("required", [])


def test_explore_reorder_keeps_a_version_groups_display_order() -> None:
    """ADR 0078: a version group is a unit like a chain; its members keep
    the order the normal ranking gave them wherever MMR puts them."""
    from datetime import UTC, datetime

    from hivemind.domain.entry import Kind
    from hivemind.services.search import explore_reorder

    def entry(eid: str, day: int) -> Entry:
        return Entry(
            id=eid,
            kind=Kind.FACT,
            summary=eid,
            author="a",
            agent="a",
            occurred_at=datetime(2026, 1, day, tzinfo=UTC),
            created_at=datetime(2026, 1, day, tzinfo=UTC),
        )

    # Display order of the group: "n" (newer) before "o"; "x" is unrelated.
    ordered = [entry("n", 2), entry("o", 1), entry("x", 3)]
    scores = {"n": 1.0, "o": 0.6, "x": 0.9}
    sims = {("n", "o"): 0.99, ("n", "x"): 0.1, ("o", "x"): 0.1}
    out = explore_reorder(ordered, scores, sims, 0.7, version_groups=[["n", "o"]])
    assert sorted(e.id for e in out) == ["n", "o", "x"]
    assert [e.id for e in out if e.id in "no"] == ["n", "o"]
    assert explore_reorder(ordered, scores, sims, 1.0, [["n", "o"]]) == ordered
