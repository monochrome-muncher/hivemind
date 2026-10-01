"""Search counters (ADR 0056): first-page searches and the ones that found
nothing, per home fleet, counted in the search service so every runner
reports them, and read back through the usage report and ``/metrics``."""

from __future__ import annotations

import pytest

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import EntryDraft, SearchCount
from hivemind.embeddings import EmbeddingError
from hivemind.memstore import MemoryStore
from hivemind.services.metrics import MetricsService
from hivemind.services.search import SearchService
from tests.fakes import make_clock, make_embedder, make_search_config
from tests.unit.test_api_endpoints import make_client, make_hivemind_app
from tests.unit.test_prometheus import samples


def _reader(fleet_id: str | None) -> Visibility:
    return Visibility(TrustLevel.CONTRIBUTOR, "reader", home_fleet_id=fleet_id)


async def _seeded() -> tuple[MemoryStore, SearchService, str]:
    clock = make_clock()
    store = MemoryStore(clock)
    fleet = await store.create_fleet("data-eng")
    await store.create_entry(
        EntryDraft(
            kind="fact",
            summary="billing deploy uses kaniko",
            author="writer",
            agent="writer",
            scope="fleet",
            fleet_id=fleet.id,
        ),
        await make_embedder().embed_text("billing deploy uses kaniko"),
    )
    service = SearchService(store, make_embedder(), make_search_config(), now_fn=clock)
    return store, service, fleet.id


async def test_counts_searches_and_misses_per_home_fleet() -> None:
    store, service, fleet_id = await _seeded()
    reader = _reader(fleet_id)
    assert (await service.search_result("billing deploy", visibility=reader)).hits
    assert not (await service.search_result("zzyzx", visibility=reader)).hits
    assert not (await service.search_result("qwxv", visibility=_reader(None))).hits
    counts = {c.fleet_id: c for c in await store.search_counts()}
    assert counts[fleet_id] == SearchCount(fleet_id, searches=2, empty=1)
    assert counts[None] == SearchCount(None, searches=1, empty=1)


async def test_later_pages_and_internal_searches_are_not_counted() -> None:
    """Paging past the end is not a miss, and a search without a caller
    (``visibility=None``: the eval harness, internal use) is not traffic."""
    store, service, fleet_id = await _seeded()
    await service.search_result("billing deploy", offset=5, visibility=_reader(fleet_id))
    await service.search_result("zzyzx")
    assert await store.search_counts() == []


async def test_a_degraded_empty_search_is_not_a_miss() -> None:
    """With the embedder down, finding nothing says more about the embedder
    than about the pool (ADR 0048): counted as a search, not as empty."""
    store, service, fleet_id = await _seeded()

    async def boom(text: str) -> list[float]:
        raise EmbeddingError("embedding endpoint is down")

    service._embedder.embed_text = boom  # type: ignore[method-assign]
    result = await service.search_result("zzyzx", visibility=_reader(fleet_id))
    assert result.degraded and not result.hits
    assert await store.search_counts() == [SearchCount(fleet_id, searches=1, empty=0)]


class _NoCountStore(MemoryStore):
    async def record_search(self, fleet_id: str | None, *, empty: bool) -> None:
        raise RuntimeError("counter table is gone")


async def test_a_failed_count_never_fails_the_search(caplog: pytest.LogCaptureFixture) -> None:
    clock = make_clock()
    store = _NoCountStore(clock)
    service = SearchService(store, make_embedder(), make_search_config(), now_fn=clock)
    result = await service.search_result("anything", visibility=_reader(None))
    assert result.hits == []
    assert "search count not recorded: RuntimeError" in caplog.text


async def test_usage_report_lists_searches_per_fleet_name() -> None:
    store, service, fleet_id = await _seeded()
    await store.create_fleet("ml")  # no searches yet: left out of by_fleet
    await service.search_result("billing deploy", visibility=_reader(fleet_id))
    await service.search_result("zzyzx", visibility=_reader(fleet_id))
    await service.search_result("zzyzx", visibility=_reader(None))
    report = await MetricsService(store).usage_report()
    assert report["searches"] == {
        "total": 3,
        "empty": 2,
        "by_fleet": {"data-eng": {"total": 2, "empty": 1}},
    }


async def test_metrics_exposes_search_counters_per_fleet() -> None:
    app = make_hivemind_app()
    fleet = await app.store.create_fleet("data-eng")
    await app.store.record_search(fleet.id, empty=True)
    await app.store.record_search(fleet.id, empty=False)
    client = make_client(app)
    async with client:
        got = samples((await client.get("/metrics")).text)
    assert got[("hivemind_searches_total", (("fleet", "data-eng"),))] == 2
    assert got[("hivemind_searches_empty_total", (("fleet", "data-eng"),))] == 1
