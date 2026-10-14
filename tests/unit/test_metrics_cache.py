"""The entry-scan cache inside ``MetricsService`` (ADR 0069).

Only ``Store.usage_counts`` (the grouped scan of ``entries``) is cached;
the agent, fleet and search reads run on every report. The clock is a
fake monotonic one, so no test sleeps.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import pytest

from hivemind.domain.access import Agent, Fleet
from hivemind.domain.entry import EntryDraft, Kind, SearchCount, UsageCount
from hivemind.memstore import MemoryStore
from hivemind.services.metrics import MetricsService
from tests.fakes import make_clock


class _Tick:
    """A fake monotonic clock (seconds)."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _CountingStore(MemoryStore):
    """Counts the reads the report makes; optionally slow or failing."""

    def __init__(self) -> None:
        super().__init__(make_clock())
        self.calls = {"usage": 0, "agents": 0, "fleets": 0, "searches": 0}
        self.fail = False
        self.delay = 0.0

    async def usage_counts(self) -> list[UsageCount]:
        self.calls["usage"] += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("scan failed")
        return await super().usage_counts()

    async def list_agents(self) -> list[Agent]:
        self.calls["agents"] += 1
        return await super().list_agents()

    async def list_fleets(self) -> list[Fleet]:
        self.calls["fleets"] += 1
        return await super().list_fleets()

    async def search_counts(self) -> list[SearchCount]:
        self.calls["searches"] += 1
        return await super().search_counts()


async def _write(store: MemoryStore, summary: str) -> None:
    await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary=summary, author="a", agent="a"), [0.1, 0.1, 0.1, 0.1]
    )


def _service(store: _CountingStore, clock: _Tick, ttl: float = 300.0) -> MetricsService:
    return MetricsService(store, entry_counts_ttl=ttl, clock=clock)


async def test_second_report_within_ttl_reuses_the_scan() -> None:
    store, clock = _CountingStore(), _Tick()
    service = _service(store, clock)
    await _write(store, "one")
    first = await service.usage_report()
    await _write(store, "two")
    clock.now += 299
    second = await service.usage_report()
    assert store.calls["usage"] == 1
    assert first.entries.total == second.entries.total == 1


async def test_scan_refreshes_after_the_ttl() -> None:
    store, clock = _CountingStore(), _Tick()
    service = _service(store, clock)
    await _write(store, "one")
    await service.usage_report()
    await _write(store, "two")
    clock.now += 300
    report = await service.usage_report()
    assert store.calls["usage"] == 2
    assert report.entries.total == 2


async def test_ttl_zero_scans_on_every_report() -> None:
    store, clock = _CountingStore(), _Tick()
    service = _service(store, clock, ttl=0)
    await service.usage_report()
    await _write(store, "one")
    report = await service.usage_report()
    assert store.calls["usage"] == 2
    assert report.entries.total == 1


async def test_concurrent_reports_share_one_scan() -> None:
    store, clock = _CountingStore(), _Tick()
    store.delay = 0.01
    service = _service(store, clock)
    await _write(store, "one")
    reports = await asyncio.gather(*(service.usage_report() for _ in range(5)))
    assert store.calls["usage"] == 1
    assert all(r.entries.total == 1 for r in reports)


async def test_a_failed_scan_is_not_cached() -> None:
    store, clock = _CountingStore(), _Tick()
    service = _service(store, clock)
    store.fail = True
    with pytest.raises(RuntimeError, match="scan failed"):
        await service.usage_report()
    store.fail = False
    await _write(store, "one")
    report = await service.usage_report()  # no clock advance: the failure left no cache
    assert store.calls["usage"] == 2
    assert report.entries.total == 1


async def test_a_failed_refresh_after_the_ttl_raises_and_retries() -> None:
    store, clock = _CountingStore(), _Tick()
    service = _service(store, clock)
    await service.usage_report()
    clock.now += 301
    store.fail = True
    with pytest.raises(RuntimeError):
        await service.usage_report()
    store.fail = False
    await _write(store, "one")
    assert (await service.usage_report()).entries.total == 1


async def test_agents_fleets_and_searches_are_read_on_every_report() -> None:
    store, clock = _CountingStore(), _Tick()
    service = _service(store, clock)
    first = await service.usage_report()
    fleet = await store.create_fleet("ml")
    await store.register_agent("alice")
    await store.record_search(fleet.id, empty=True)
    second = await service.usage_report()
    assert store.calls == {"usage": 1, "agents": 2, "fleets": 2, "searches": 2}
    assert first.agents.total == 0
    assert second.agents.total == 1
    assert second.fleets.total == 1
    assert asdict(second.searches.by_fleet["ml"]) == {"total": 1, "empty": 1, "explore": 0}
