"""The portal's fleet activity chart (ADR 0165): ``GET /v1/portal/fleet-activity``."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from hivemind.admin.app import STATIC_DIR as ADMIN_STATIC
from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.memstore import MemoryStore
from hivemind.portal.app import STATIC_DIR as PORTAL_STATIC
from hivemind.ports import Credential
from hivemind.services.fleet_activity import CACHE_SIZE, FleetActivityService
from tests.builders import make_hivemind_app
from tests.fakes import FIXED_NOW, make_client, make_clock, make_embedder

CAROL = {"X-API-Key": "key-carol"}
LONE = {"X-API-Key": "key-lone"}  # no home fleet
TODAY = FIXED_NOW.date().isoformat()


async def _app():  # type: ignore[no-untyped-def]
    app = make_hivemind_app()
    app.fleet_activity_service._wall_clock = lambda: FIXED_NOW
    blue = await app.store.create_fleet("blue")
    for name in ("carol", "bob", "alice"):
        await app.store.register_agent(name)
        await app.store.activate_agent(
            name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=blue.id
        )
    await app.store.register_agent("idle")
    await app.store.activate_agent("idle", trust_level=TrustLevel.LURKER, home_fleet_id=blue.id)
    for key, name, fleet in (("key-carol", "carol", blue.id), ("key-lone", "lone", None)):
        app.authenticator.add_key(  # type: ignore[attr-defined]
            key,
            Credential(
                user_id=name,
                agent_name=name,
                access_controlled=True,
                trust_level=TrustLevel.CONTRIBUTOR,
                home_fleet_id=fleet,
            ),
        )
    return app, blue


async def _write(app, author: str, scope: str = "org"):  # type: ignore[no-untyped-def]
    summary = f"{author} {scope} note"
    return await app.store.create_entry(
        EntryDraft(kind=Kind.FACT, summary=summary, author=author, agent="a", scope=scope),
        await make_embedder().embed_text(summary),
    )


async def _rate(app, entry, rater: str) -> None:  # type: ignore[no-untyped-def]
    await app.store.record_feedback(
        Feedback(entry.id, rater, rater, Verdict.HELPFUL, None, FIXED_NOW)
    )


async def test_one_zero_filled_row_per_day_for_each_active_agent_sorted_by_name() -> None:
    app, blue = await _app()
    await _write(app, "bob")
    mine = await _write(app, "alice")
    await _write(app, "alice")
    await _rate(app, mine, "bob")
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/fleet-activity", params={"days": 7}, headers=CAROL)
    assert resp.status_code == 200
    body = resp.json()
    assert (body["fleet_id"], body["days"], body["quiet"]) == (blue.id, 7, 2)
    assert body["since"] == (FIXED_NOW - timedelta(days=6)).date().isoformat() + "T00:00:00Z"
    assert [(a["name"], a["writes"], a["ratings"]) for a in body["agents"]] == [
        ("alice", 2, 0),
        ("bob", 1, 1),
    ]
    daily = body["agents"][1]["daily"]
    assert len(daily) == 7
    assert [d["day"] for d in daily][-1] == TODAY
    assert [(d["writes"], d["ratings"]) for d in daily] == [(0, 0)] * 6 + [(1, 1)]


async def test_writes_count_only_what_the_key_can_read() -> None:
    app, _blue = await _app()
    await _write(app, "bob", scope="self")
    await _write(app, "alice")
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/fleet-activity", headers=CAROL)
    body = resp.json()
    assert body["days"] == 30
    assert [(a["name"], a["writes"]) for a in body["agents"]] == [("alice", 1)]
    assert body["quiet"] == 3  # bob's only memory is private, so carol, bob and idle show none


async def test_a_dormant_key_can_read_nothing_so_nobody_is_listed_or_quiet() -> None:
    app, blue = await _app()
    await _write(app, "alice")
    app.authenticator.add_key(  # type: ignore[attr-defined]
        "key-zed",
        Credential(
            user_id="zed",
            agent_name="zed",
            access_controlled=True,
            trust_level=TrustLevel.UNTRUSTED,
            home_fleet_id=blue.id,
        ),
    )
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/fleet-activity", headers={"X-API-Key": "key-zed"})
    assert resp.status_code == 200
    assert (resp.json()["agents"], resp.json()["quiet"]) == ([], 0)


async def test_ratings_count_on_readable_entries_on_the_latest_day_and_not_when_set_aside() -> None:
    app, _blue = await _app()
    readable = await _write(app, "alice")
    private = await _write(app, "alice", scope="self")
    await _rate(app, readable, "bob")
    await _rate(app, private, "bob")  # an entry the key cannot read
    await _rate(app, readable, "alice")
    await app.store.record_feedback(
        Feedback(readable.id, "bob", "bob", Verdict.WRONG, None, FIXED_NOW - timedelta(days=2))
    )  # bob's one rating of it moves to its latest day
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/fleet-activity", params={"days": 7}, headers=CAROL)
    by_name = {a["name"]: a for a in resp.json()["agents"]}
    assert by_name["bob"]["ratings"] == 1
    assert by_name["bob"]["daily"][-3]["ratings"] == 1  # two days before today
    assert by_name["alice"]["ratings"] == 1


async def test_the_verdict_mix_is_for_the_whole_fleet_never_per_agent() -> None:
    app, _blue = await _app()
    first, second = await _write(app, "alice"), await _write(app, "alice")
    await _rate(app, first, "bob")
    await _rate(app, second, "carol")
    await app.store.record_feedback(
        Feedback(first.id, "carol", "carol", Verdict.STALE, None, FIXED_NOW)
    )
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/fleet-activity", params={"days": 1}, headers=CAROL)
    body = resp.json()
    assert body["verdicts"] == {"helpful": 2, "stale": 1, "wrong": 0}
    assert all("helpful" not in agent for agent in body["agents"])


async def test_a_key_without_a_home_fleet_gets_no_agents() -> None:
    app, _blue = await _app()
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/fleet-activity", headers=LONE)
    assert resp.status_code == 200
    assert (resp.json()["fleet_id"], resp.json()["agents"], resp.json()["quiet"]) == (None, [], 0)


@pytest.mark.parametrize("days", [0, 91, "x"])
async def test_days_is_one_to_ninety(days: object) -> None:
    app, _blue = await _app()
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/fleet-activity", params={"days": days}, headers=CAROL)
    assert resp.status_code == 422


# -- the chart's series (fleetchart.js, run as it ships) --------------------------------


def _chart_script(tmp_path: Path, script: str) -> object:
    """Run ``script`` against a copy of fleetchart.js beside the shared modules it imports."""
    for source in (
        PORTAL_STATIC / "fleetchart.js",
        ADMIN_STATIC / "ui.js",
        ADMIN_STATIC / "charts.js",
    ):
        shutil.copy(source, tmp_path / source.name)
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, (tmp_path / "fleetchart.js").as_uri()],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(out.stdout)


AGENTS_JS = """
const agent = (name, writes, ratings) => ({ name, writes, ratings, daily: [
  { day: "2026-06-01", writes: 0, ratings: 0 }, { day: "2026-06-02", writes, ratings }] });
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_every_agent_keeps_its_colour_when_the_measure_flips(tmp_path: Path) -> None:
    script = (
        AGENTS_JS
        + """
    const { chartSeries } = await import(process.argv[1])
    const agents = [agent("zed", 9, 0), agent("amy", 0, 5), agent("bob", 3, 3)]
    const slots = (metric) => Object.fromEntries(chartSeries(agents, metric).map((s) => [s.name, s.slot]))
    console.log(JSON.stringify({ writes: slots("writes"), ratings: slots("ratings") }))
    """
    )
    got = _chart_script(tmp_path, script)
    assert got["writes"] == got["ratings"] == {"amy": "1", "bob": "2", "zed": "3"}  # type: ignore[call-overload]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_more_than_seven_agents_fold_the_least_active_into_one_gray_stack(tmp_path: Path) -> None:
    script = (
        AGENTS_JS
        + """
    const { chartSeries } = await import(process.argv[1])
    const agents = "abcdefghij".split("").map((name, i) => agent(name, i + 1, 0))
    const series = chartSeries(agents, "writes")
    console.log(JSON.stringify(series.map((s) => [s.name, s.slot, s.values[1]])))
    """
    )
    got = _chart_script(tmp_path, script)
    assert got == [  # the seven busiest (d..j) by name, then the three others summed (1 + 2 + 3)
        ["d", "1", 4], ["e", "2", 5], ["f", "3", 6], ["g", "4", 7],
        ["h", "5", 8], ["i", "6", 9], ["j", "7", 10], ["Other agents", "other", 6],
    ]  # fmt: skip


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_verdict_split_rounds_each_share(tmp_path: Path) -> None:
    script = """
    const { verdictSplit } = await import(process.argv[1])
    console.log(JSON.stringify([verdictSplit({ helpful: 6, stale: 2, wrong: 1 }), verdictSplit({ helpful: 0, stale: 0, wrong: 0 })]))
    """
    split, empty = _chart_script(tmp_path, script)  # type: ignore[misc]
    assert split["total"] == 9
    assert [(p["key"], p["count"], p["share"]) for p in split["parts"]] == [
        ("helpful", 6, 67), ("stale", 2, 22), ("wrong", 1, 11),
    ]  # fmt: skip
    assert empty["total"] == 0 and {p["share"] for p in empty["parts"]} == {0}


# -- the service: one day, zero totals and the cache ------------------------------------


async def test_a_window_of_one_day_is_today_only() -> None:
    app, _blue = await _app()
    await _write(app, "alice")
    async with make_client(app) as client:
        resp = await client.get("/v1/portal/fleet-activity", params={"days": 1}, headers=CAROL)
    (agent,) = resp.json()["agents"]
    assert [(d["day"], d["writes"]) for d in agent["daily"]] == [(TODAY, 1)]


class _CountingStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(make_clock())
        self.reads = 0
        self.fail = False

    async def fleet_activity(self, fleet_id, since, until, visibility):  # type: ignore[no-untyped-def]
        self.reads += 1
        if self.fail:
            raise RuntimeError("down")
        return await super().fleet_activity(fleet_id, since, until, visibility)


def _service(store: _CountingStore, now: list[float]) -> FleetActivityService:
    return FleetActivityService(store, ttl=30, clock=lambda: now[0], wall_clock=lambda: FIXED_NOW)


def _key(name: str, fleet: str = "f") -> Visibility:
    return Visibility(TrustLevel.CONTRIBUTOR, name, home_fleet_id=fleet)


async def test_a_report_is_reused_within_the_ttl_per_key_and_window() -> None:
    store, now = _CountingStore(), [0.0]
    service = _service(store, now)
    await service.report(_key("a"), 30)
    await service.report(_key("a"), 30)
    assert store.reads == 1
    await service.report(_key("a"), 7)
    await service.report(_key("b"), 30)
    await service.report(Visibility(TrustLevel.CONTRIBUTOR, "a", "f", inherited=("x",)), 30)
    assert store.reads == 4
    now[0] = 31.0
    await service.report(_key("a"), 30)
    assert store.reads == 5


async def test_a_failed_read_is_not_cached_and_invalidate_drops_the_reports() -> None:
    store, now = _CountingStore(), [0.0]
    service = _service(store, now)
    store.fail = True
    with pytest.raises(RuntimeError):
        await service.report(_key("a"), 30)
    store.fail = False
    await service.report(_key("a"), 30)
    await service.report(_key("a"), 30)
    assert store.reads == 2
    service.invalidate()
    await service.report(_key("a"), 30)
    assert store.reads == 3


async def test_the_cache_is_bounded() -> None:
    store, now = _CountingStore(), [0.0]
    service = _service(store, now)
    for i in range(CACHE_SIZE + 5):
        await service.report(_key(f"k{i}"), 30)
    assert len(service._cache) == CACHE_SIZE
