"""The admin activity view (ADR 0080): the report shape, the window, the
revoked-reporter rule, the TTL cache and the "observation only" rule."""

from __future__ import annotations

import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest

from hivemind.domain.access import TrustLevel
from hivemind.domain.activity import Activity
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.memstore import MemoryStore
from hivemind.services.activity import ActivityService
from tests.fakes import make_clock
from tests.unit.test_api_endpoints import make_client, make_hivemind_app

ADMIN = {"X-API-Key": "key-admin"}
NOW = datetime(2026, 6, 10, 15, 30, tzinfo=UTC)


class _Tick:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _CountingStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(make_clock(NOW))
        self.calls: list[datetime] = []
        self.fail = False

    async def activity(self, since: datetime) -> Activity:
        self.calls.append(since)
        if self.fail:
            raise RuntimeError("scan failed")
        return await super().activity(since)


async def _write(store: MemoryStore, summary: str, author: str, at: datetime, **kw: object):
    store._clock.set(at)  # type: ignore[attr-defined]
    return await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary=summary, author=author, agent=author, **kw),  # type: ignore[arg-type]
        [0.1, 0.1, 0.1, 0.1],
    )


async def _verdict(store: MemoryStore, entry_id: str, user: str, v: Verdict, at: datetime) -> None:
    await store.record_feedback(Feedback(entry_id, user, user, v, None, at))


def _service(store: MemoryStore, tick: _Tick | None = None, ttl: float = 300.0) -> ActivityService:
    return ActivityService(store, ttl=ttl, clock=tick or _Tick(), wall_clock=lambda: NOW)


async def _seeded() -> _CountingStore:
    store = _CountingStore()
    fleet = await store.create_fleet("blue")
    await store.register_agent("scout")
    await store.activate_agent("scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id)
    await store.register_agent("ghost")
    await store.activate_agent("ghost", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id)
    day = timedelta(days=1)
    old = await _write(store, "old", "scout", NOW - 40 * day, scope="fleet", fleet_id=fleet.id)
    a = await _write(store, "a", "scout", NOW - 2 * day, scope="fleet", fleet_id=fleet.id)
    await _write(store, "b", "admin", NOW)
    await _verdict(store, a.id, "ghost", Verdict.HELPFUL, NOW - day)
    await _verdict(store, a.id, "mira", Verdict.WRONG, NOW - day)
    await _verdict(store, old.id, "mira", Verdict.STALE, NOW - 35 * day)
    await store.revoke_agent("ghost")
    return store


async def test_report_zero_fills_every_day_and_totals_the_window() -> None:
    report = await _service(await _seeded()).report(7)
    days = [d["day"].isoformat() for d in report["daily"]]
    assert days == [f"2026-06-{n:02d}" for n in range(4, 11)]
    assert report["since"] == datetime(2026, 6, 4, tzinfo=UTC)
    by_day = {d["day"].isoformat(): d for d in report["daily"]}
    assert by_day["2026-06-08"] == {
        "day": datetime(2026, 6, 8).date(),
        "writes": 1,
        "helpful": 0,
        "stale": 0,
        "wrong": 0,
    }
    # ghost is revoked: its helpful verdict on 06-09 is not counted
    assert (by_day["2026-06-09"]["helpful"], by_day["2026-06-09"]["wrong"]) == (0, 1)
    assert by_day["2026-06-05"]["writes"] == 0
    assert report["totals"] == {"writes": 2, "verdicts": 1, "writers": 2, "reporters": 1}


async def test_agent_rows_cover_registered_authors_and_reporters() -> None:
    report = await _service(await _seeded()).report(7)
    rows = {r["name"]: r for r in report["agents"]}
    assert set(rows) == {"scout", "ghost", "admin", "mira"}
    scout = rows["scout"]
    assert (scout["registered"], scout["status"], scout["home_fleet"]) == (True, "active", "blue")
    assert (scout["entries"], scout["active"], scout["recent_writes"]) == (2, 2, 1)
    assert scout["received"] == {"helpful": 0, "stale": 1, "wrong": 1}
    assert rows["admin"]["registered"] is False and rows["admin"]["recent_writes"] == 1
    assert rows["mira"]["verdicts_given"] == 2 and rows["mira"]["recent_verdicts"] == 1
    assert rows["ghost"]["verdicts_given"] == 0 and rows["ghost"]["status"] == "revoked"
    assert [r["name"] for r in report["agents"]][:2] == ["admin", "scout"]  # recent_writes, name


async def test_fleet_rows_include_empty_fleets_and_the_fleetless_row() -> None:
    store = await _seeded()
    await store.create_fleet("idle")
    report = await _service(store).report(7)
    rows = {r["name"]: r for r in report["fleets"]}
    assert rows["blue"]["entries"] == 2 and rows["blue"]["recent_writes"] == 1
    assert rows["blue"]["writers"] == 1 and rows["blue"]["agents"] == 1  # ghost is revoked
    assert rows["idle"]["entries"] == 0 and rows["idle"]["last_write_at"] is None
    assert rows[None]["fleet_id"] is None and rows[None]["entries"] == 1


async def test_cache_honours_the_ttl_and_is_keyed_by_days() -> None:
    store, tick = await _seeded(), _Tick()
    service = _service(store, tick)
    await service.report(7)
    await service.report(7)
    assert len(store.calls) == 1
    await service.report(30)
    assert len(store.calls) == 2
    tick.now += 299
    await service.report(7)
    assert len(store.calls) == 2
    tick.now += 2
    await service.report(7)
    assert len(store.calls) == 3


async def test_cache_does_not_straddle_utc_midnight() -> None:
    store, tick = await _seeded(), _Tick()
    wall = [datetime(2026, 6, 10, 23, 59, tzinfo=UTC)]
    service = ActivityService(store, ttl=300.0, clock=tick, wall_clock=lambda: wall[0])
    before = await service.report(3)
    assert [d["day"].isoformat() for d in before["daily"]] == [
        "2026-06-08",
        "2026-06-09",
        "2026-06-10",
    ]
    wall[0] = datetime(2026, 6, 11, 0, 1, tzinfo=UTC)
    tick.now += 60  # well inside the TTL
    after = await service.report(3)
    assert len(store.calls) == 2  # the new date refilled the cache
    assert [d["day"].isoformat() for d in after["daily"]] == [
        "2026-06-09",
        "2026-06-10",
        "2026-06-11",
    ]
    assert len(service._cache) == 1  # the other date's entry was dropped


async def test_a_failed_read_is_not_cached() -> None:
    store, tick = await _seeded(), _Tick()
    service = _service(store, tick)
    store.fail = True
    with pytest.raises(RuntimeError):
        await service.report(7)
    store.fail = False
    assert (await service.report(7))["totals"]["writes"] == 2
    assert len(store.calls) == 2


async def test_endpoint_validates_days_and_serves_the_report() -> None:
    app = make_hivemind_app()
    async with make_client(app) as client:
        ok = await client.get("/v1/admin/activity", params={"days": 3}, headers=ADMIN)
        default = await client.get("/v1/admin/activity", headers=ADMIN)
        low = await client.get("/v1/admin/activity", params={"days": 0}, headers=ADMIN)
        high = await client.get("/v1/admin/activity", params={"days": 366}, headers=ADMIN)
    body = ok.json()
    assert ok.status_code == 200 and body["days"] == 3 and len(body["daily"]) == 3
    assert set(body) == {"days", "since", "daily", "totals", "agents", "fleets"}
    assert default.json()["days"] == 30
    assert low.status_code == high.status_code == 422


def test_nothing_agent_facing_reads_the_activity_aggregates() -> None:
    """Observation only (ADR 0080): search, ranking, scoring, quality, the
    briefing and the MCP tools must not reach the activity view."""
    root = pathlib.Path(__file__).parents[2] / "src" / "hivemind"
    readers = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if re.search(
            r"\b(activity_service|ActivityService|AuthorActivity|ReporterActivity|DayActivity"
            r"|ReceivedFeedback|store\.activity|def activity)\b",
            path.read_text(),
        )
    }
    assert readers <= {
        "ports.py",
        "memstore.py",
        "store/pgstore.py",
        "services/activity.py",
        "services/wiring.py",
        "domain/activity.py",
        "api/routes.py",
        "api/deps.py",
        "api/main.py",
    }
    for path in ("services/search.py", "retrieval", "mcp"):
        target = root / path
        files = [target] if target.is_file() else list(target.rglob("*.py"))
        assert all("activity" not in f.read_text().lower().replace("inactive", "") for f in files)
