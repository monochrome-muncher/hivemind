"""Fleet health metrics (ADR 0075): the report shape, the cache, the
Prometheus gauges and the "observation only" rule. The aggregates
themselves are pinned with hand-computed values by
``tests/integration/test_store_contract.py``."""

from __future__ import annotations

import pathlib
import re
from dataclasses import asdict
from datetime import timedelta

from prometheus_client.parser import text_string_to_metric_families

from hivemind.api.prometheus import _health_families
from hivemind.domain.entry import EntryDraft, Kind, Source, SourceType
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.memstore import MemoryStore
from hivemind.services.metrics import MetricsService
from tests.fakes import make_clock
from tests.unit.test_api_endpoints import make_client, make_hivemind_app

VEC = [0.1, 0.1, 0.1, 0.1]
ADMIN = {"X-API-Key": "key-admin"}


async def _write(store: MemoryStore, summary: str, **kw: object):
    draft = EntryDraft(kind=Kind.FACT, summary=summary, author="a", agent="a", **kw)  # type: ignore[arg-type]
    return await store.create_entry(draft, VEC)


async def test_an_empty_pool_reports_unknown_shares_not_zero() -> None:
    health = (await MetricsService(MemoryStore(make_clock())).usage_report()).health
    assert asdict(health.helpful_with_note_share) == {"count": 0, "total": 0, "share": None}
    assert health.sourced_share.share is None
    assert health.correction_latency_p50_seconds is None
    assert health.supersessions_per_100_writes.per_100 is None
    assert health.gap_tag_counts == {"anomaly": 0, "open-question": 0, "dead-end": 0}
    assert health.by_fleet == {}
    assert asdict(health.echo_rate) == {"count": 0, "total": 0, "share": None}
    assert health.topic_dispersion == []
    assert asdict(health.rival_lifetime.alternative) == {"count": 0, "median_age_seconds": None}
    assert health.grounding_share.share is None
    assert health.top3_agent_label_write_share.labels == 0
    assert asdict(health.sibling_verdict_share) == {"count": 0, "total": 0, "share": None}
    assert asdict(health.top3_owner_verdict_share) == {
        "count": 0, "total": 0, "share": None, "owners": 0,
    }  # fmt: skip


async def test_convergence_figures_reach_the_report_and_the_gauges() -> None:
    store = MemoryStore(make_clock())
    for i in range(3):
        last = await store.create_entry(
            EntryDraft(
                kind=Kind.INSIGHT,
                summary=("Conflict: " if i == 0 else "") + f"s{i}",
                author="a",
                agent="L1",
                tags=("alternative",),
                sources=(Source(SourceType.URL, "https://example.test"),),
            ),
            VEC,
        )
    report = await MetricsService(store, wall_clock=lambda: last.created_at).usage_report()
    body = report.health
    assert asdict(body.echo_rate) == {"count": 2, "total": 3, "share": 2 / 3}  # identical vectors
    assert body.grounding_share.share == 1.0
    assert body.rival_lifetime.alternative.count == 3
    assert body.rival_lifetime.conflict.count == 1
    assert asdict(body.top3_agent_label_write_share) == {
        "count": 3, "total": 3, "share": 1.0, "labels": 1,
    }  # fmt: skip
    got = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in _health_families(body)
        for s in family.samples
    }
    assert got[("hivemind_health_rival_entries", (("kind", "alternative"),))] == 3
    assert got[("hivemind_health_echo_ratio", ())] == 2 / 3
    assert ("hivemind_health_rival_median_age_seconds", (("kind", "contested"),)) not in got


async def test_the_report_folds_the_store_aggregates() -> None:
    store = MemoryStore(make_clock())
    fleet = await store.create_fleet("blue")
    src = (Source(SourceType.URL, "https://example.test"),)
    await _write(store, "sourced", scope="fleet", fleet_id=fleet.id, sources=src)
    old = await _write(store, "unsourced", scope="fleet", fleet_id=fleet.id, tags=("anomaly",))
    new = await _write(store, "v2", scope="fleet", fleet_id=fleet.id, supersedes=(old.id,))
    flag = new.created_at - timedelta(hours=2)
    await store.record_feedback(Feedback(old.id, "r", "r", Verdict.WRONG, None, flag))
    await store.record_feedback(Feedback(new.id, "r", "r", Verdict.HELPFUL, "checked", flag))
    await store.record_feedback(Feedback(new.id, "s", "s", Verdict.HELPFUL, None, flag))
    report = await MetricsService(store, wall_clock=lambda: new.created_at).usage_report()
    health = report.health
    assert asdict(health.helpful_with_note_share) == {"count": 1, "total": 2, "share": 0.5}
    assert health.sourced_share.count == 1  # "v2" and the sourced one are sourceable
    assert health.sourced_share.total == 2
    assert health.correction_latency_p50_seconds == 7200.0
    assert asdict(health.supersessions_per_100_writes) == {
        "supersessions": 1,
        "writes": 3,
        "per_100": 100 / 3,
    }
    assert health.by_fleet["blue"].sourced_share.share == 0.5
    assert health.by_fleet["blue"].gap_tag_counts["anomaly"] == 0  # superseded: not active


async def test_owner_level_shares_reach_the_report_the_rest_body_and_the_gauges() -> None:
    app = make_hivemind_app()
    store = app.store
    for name, alias in (("a1", "Ann"), ("a2", "ann"), ("b1", None)):
        await store.register_agent(name, alias)
    by_a2 = await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="by a2", author="a2", agent="a2"), VEC
    )
    by_b1 = await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="by b1", author="b1", agent="b1"), VEC
    )
    at = max(by_a2.created_at, by_b1.created_at)
    for entry, user in ((by_a2, "a1"), (by_a2, "b1"), (by_b1, "a1"), (by_b1, "a2")):
        await store.record_feedback(Feedback(entry.id, user, user, Verdict.HELPFUL, None, at))
    report = await MetricsService(store, wall_clock=lambda: at).usage_report()
    body = report.health
    assert asdict(body.sibling_verdict_share) == {"count": 1, "total": 4, "share": 0.25}
    assert asdict(body.top3_owner_verdict_share) == {
        "count": 4, "total": 4, "share": 1.0, "owners": 2,
    }  # fmt: skip
    got = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in _health_families(body)
        for s in family.samples
    }
    assert got[("hivemind_health_sibling_verdict_ratio", ())] == 0.25
    assert got[("hivemind_health_top3_owner_verdict_ratio", ())] == 1.0
    client = make_client(app)
    async with client:
        rest = (await client.get("/v1/metrics", headers=ADMIN)).json()["health"]
    assert rest["sibling_verdict_share"] == {"count": 0, "total": 0, "share": None}
    assert rest["top3_owner_verdict_share"] == {"count": 0, "total": 0, "share": None, "owners": 0}


def test_the_admin_health_card_shows_the_owner_figures_with_the_caveat() -> None:
    js = (pathlib.Path(__file__).parents[2] / "src/hivemind/admin/static/health.js").read_text()
    assert "health.sibling_verdict_share" in js and "health.top3_owner_verdict_share" in js
    assert "self-reported" in js


class _CountingStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(make_clock())
        self.health_calls = 0

    async def fleet_health(self, now):  # type: ignore[no-untyped-def]
        self.health_calls += 1
        return await super().fleet_health(now)


async def test_health_is_cached_with_the_entry_scan_under_the_same_ttl() -> None:
    store = _CountingStore()
    tick = [0.0]
    service = MetricsService(store, entry_counts_ttl=300.0, clock=lambda: tick[0])
    await service.usage_report()
    await service.usage_report()
    assert store.health_calls == 1
    tick[0] = 301.0
    await service.usage_report()
    assert store.health_calls == 2


async def test_rest_metrics_carry_health_for_the_admin_key_only() -> None:
    app = make_hivemind_app()
    await app.store.create_fleet("blue")
    client = make_client(app)
    async with client:
        body = (await client.get("/v1/metrics", headers=ADMIN)).json()
        denied = await client.get("/v1/metrics", headers={"X-API-Key": "key-alice"})
    assert denied.status_code == 403
    assert body["health"]["sourced_share"] == {"count": 0, "total": 0, "share": None}
    assert body["health"]["by_fleet"]["blue"]["floating_gap"] == 0


async def test_prometheus_exposes_the_health_gauges() -> None:
    app = make_hivemind_app()
    fleet = await app.store.create_fleet("blue")
    await _write(app.store, "tagged", scope="fleet", fleet_id=fleet.id, tags=("dead-end",))
    client = make_client(app)
    async with client:
        text = (await client.get("/metrics")).text
    got = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }
    assert got[("hivemind_health_gap_entries", (("tag", "dead-end"),))] == 1
    assert got[("hivemind_health_fleet_gap_entries", (("fleet", "blue"), ("tag", "dead-end")))] == 1
    assert got[("hivemind_health_sourced_ratio", ())] == 0.0
    assert got[("hivemind_health_orphaned_flagged", ())] == 0
    # No verdicts yet: shares without a denominator have no series.
    assert ("hivemind_health_helpful_with_note_ratio", ()) not in got
    assert ("hivemind_health_correction_latency_p50_seconds", ()) not in got


_ALLOWED = {
    "ports.py",
    "memstore.py",
    "store/pgstore.py",
    "services/metrics.py",
    "domain/health.py",
}


def test_nothing_but_the_metrics_report_reads_the_health_aggregates() -> None:
    """Observation only (ADR 0075): search, ranking, scoring, quality, the
    briefing and the MCP tools must not reach ``fleet_health``."""
    root = pathlib.Path(__file__).parents[2] / "src" / "hivemind"
    readers = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if re.search(
            r"\b(fleet_health|FleetHealth|HealthCounts|fleet_convergence|Convergence|TagDispersion)\b",
            path.read_text(),
        )
    }
    assert readers <= _ALLOWED
    # and the report's only consumers are the two admin surfaces
    consumers = {
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if re.search(r"""\[["']health["']\]""", path.read_text())
    }
    assert consumers <= {"api/routes.py", "services/metrics.py"}


class _FailingHealthStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(make_clock())
        self.fail_health = False
        self.fail_convergence = False
        self.usage_calls = 0
        self.health_calls = 0

    async def usage_counts(self):  # type: ignore[no-untyped-def]
        self.usage_calls += 1
        return await super().usage_counts()

    async def fleet_health(self, now):  # type: ignore[no-untyped-def]
        self.health_calls += 1
        if self.fail_health:
            raise RuntimeError("boom")
        return await super().fleet_health(now)

    async def fleet_convergence(self, now):  # type: ignore[no-untyped-def]
        if self.fail_convergence:
            raise RuntimeError("boom")
        return await super().fleet_convergence(now)


async def test_a_health_failure_does_not_fail_the_usage_report() -> None:
    store = _FailingHealthStore()
    tick = [0.0]
    service = MetricsService(store, entry_counts_ttl=300.0, clock=lambda: tick[0])
    store.fail_health = True
    report = await service.usage_report()  # no exception
    assert report.health is None and asdict(report.health_status) == {"ok": False}
    assert report.entries.total == 0
    # The failure is not cached: the next report retries, and recovers.
    store.fail_health = False
    report = await service.usage_report()
    assert report.health is not None and asdict(report.health_status) == {"ok": True}
    assert (store.usage_calls, store.health_calls) == (1, 2)  # the entry scan stayed cached
    # Later failures serve the last good values and say they are stale.
    tick[0] = 301.0
    store.fail_convergence = True
    report = await service.usage_report()
    assert report.health is not None and asdict(report.health_status) == {"ok": False}


async def test_prometheus_reports_health_success_and_leaks_no_tag_strings() -> None:
    store = _FailingHealthStore()
    store.fail_health = True
    report = await MetricsService(store).usage_report()
    got = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in __import__("hivemind.api.prometheus", fromlist=["x"])._usage_families(report)
        for s in family.samples
    }
    assert got[("hivemind_health_success", ())] == 0.0
    assert not any(
        k[0].startswith("hivemind_health_") and k[0] != "hivemind_health_success" for k in got
    )
    # only the fixed gap tags ever appear as a tag label
    body = (await MetricsService(MemoryStore(make_clock())).usage_report()).health
    labels = {
        v
        for family in _health_families(body)
        for sample in family.samples
        for k, v in sample.labels.items()
        if k == "tag"
    }
    assert labels <= {"anomaly", "open-question", "dead-end"}
