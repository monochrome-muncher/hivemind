"""The Prometheus scrape endpoint of the REST runner (ADR 0050).

``GET /metrics`` is unauthenticated, so it must stay inside the cluster: the optional Ingress sends
it to the source-allowlisted ``hivemind-admin-api`` Ingress, never the public one.

Series:

* Usage gauges from ``MetricsService.usage_report``, cached for ``USAGE_TTL_SECONDS`` with one
  refresh at a time. The entry scan is cached again in ``MetricsService``
  (``HIVEMIND_METRICS_ENTRY_COUNTS_TTL``, ADR 0069), so entry gauges can lag that long. Every
  replica reports the same pool: aggregate with ``max``, not ``sum``.
* HTTP request metrics of this process, per method and route template (bounded labels).
* Degraded searches (ADR 0048), search counters per fleet (ADR 0056, 0078; read from the pool, so
  MCP is included), flag confirmation per agent (ADR 0068) and fleet health gauges (ADR 0075). All
  are observations, never ranking inputs; no denominator means no series.

A failed usage refresh keeps the last snapshot and sets ``hivemind_usage_report_success`` to 0; the
scrape still answers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.exposition import generate_latest
from prometheus_client.registry import Collector

from hivemind.domain.usage import FleetHealthUsage, HealthUsage, RivalLifetime, UsageReport
from hivemind.http_metrics import UNMATCHED_ROUTE, ProcessMetrics, RequestMetrics

logger = logging.getLogger(__name__)

METRICS_PATH = "/metrics"
USAGE_TTL_SECONDS = 30.0

Clock = Callable[[], float]


class _UsageCollector(Collector):
    """Yields the cached usage snapshot as gauges (read at scrape time)."""

    def __init__(self, owner: PrometheusMetrics) -> None:
        self._owner = owner

    def collect(self) -> Iterator[GaugeMetricFamily | CounterMetricFamily]:
        ok = GaugeMetricFamily(
            "hivemind_usage_report_success",
            "1 if the last usage-report refresh succeeded, else 0",
        )
        ok.add_metric([], 1.0 if self._owner.usage_ok else 0.0)
        yield ok
        report = self._owner.usage_snapshot
        if report is not None:
            yield from _usage_families(report)


def _gauge(name: str, doc: str, labels: list[str], values: Mapping[Any, Any]) -> GaugeMetricFamily:
    family = GaugeMetricFamily(name, doc, labels=labels)
    for key, value in values.items():
        family.add_metric([str(key)] if labels else [], float(value))
    return family


def _usage_families(report: UsageReport) -> Iterator[GaugeMetricFamily | CounterMetricFamily]:
    entries, fleets, agents = report.entries, report.fleets, report.agents
    yield _gauge(
        "hivemind_entries",
        "Entries in the pool, by state (inactive = superseded or withdrawn)",
        ["state"],
        {"active": entries.active, "inactive": entries.inactive},
    )
    yield _gauge(
        "hivemind_entries_by_scope", "Entries by scope (all states)", ["scope"], entries.by_scope
    )
    yield _gauge(
        "hivemind_entries_by_kind", "Entries by kind (all states)", ["kind"], entries.by_kind
    )
    yield _gauge(
        "hivemind_entries_by_importance_source",
        "Entries by whether importance was caller-supplied or the default",
        ["importance_source"],
        entries.by_importance_source,
    )
    by_author_kind = GaugeMetricFamily(
        "hivemind_entries_by_author_kind",
        "Entries per registered agent and kind (all states)",
        labels=["author", "kind"],
    )
    for author, kinds in entries.by_author_kind.items():
        for kind, count in kinds.items():
            by_author_kind.add_metric([author, kind], float(count))
    yield by_author_kind
    fleet_total = GaugeMetricFamily("hivemind_fleets", "Fleets")
    fleet_total.add_metric([], float(fleets.total))
    yield fleet_total
    yield _gauge(
        "hivemind_fleet_entries",
        "Entries written into each fleet (all states)",
        ["fleet"],
        fleets.writes_by_fleet,
    )
    in_phase = GaugeMetricFamily(
        "hivemind_fleet_phase",
        "1 for each fleet with a phase in force (ADR 0143)",
        labels=["fleet", "phase"],
    )
    for fleet, phase in fleets.phase_by_fleet.items():
        in_phase.add_metric([fleet, phase.phase], 1.0)
    yield in_phase
    yield _gauge(
        "hivemind_agents",
        "Registered agents, by status",
        ["status"],
        {"pending": agents.pending, "active": agents.active, "revoked": agents.revoked},
    )
    yield _gauge(
        "hivemind_agents_by_trust_level",
        "Registered agents, by trust level (all statuses)",
        ["trust_level"],
        agents.by_trust_level,
    )
    yield _gauge(
        "hivemind_agents_by_role",
        "Registered agents that are not revoked, by role (ADR 0144)",
        ["role"],
        agents.by_role,
    )
    by_fleet = report.searches.by_fleet
    searches = CounterMetricFamily(
        "hivemind_searches",
        "First-page searches by agents of each fleet, all runners (ADR 0056)",
        labels=["fleet"],
    )
    empty = CounterMetricFamily(
        "hivemind_searches_empty",
        "First-page searches by agents of each fleet that found nothing (ADR 0056)",
        labels=["fleet"],
    )
    explored = CounterMetricFamily(
        "hivemind_searches_explore",
        "First-page explore searches by agents of each fleet (ADR 0078)",
        labels=["fleet"],
    )
    for fleet, counts in by_fleet.items():
        searches.add_metric([fleet], float(counts.total))
        empty.add_metric([fleet], float(counts.empty))
        explored.add_metric([fleet], float(counts.explore))
    yield searches
    yield empty
    yield explored
    by_agent = report.feedback.by_agent
    yield _gauge(
        "hivemind_feedback_flags",
        "Stale or wrong reports per registered agent, made while the entry was current (ADR 0068)",
        ["agent"],
        {a: c.flags for a, c in by_agent.items()},
    )
    yield _gauge(
        "hivemind_feedback_flags_confirmed",
        "Of those flags, the ones whose entry was superseded afterwards (ADR 0068)",
        ["agent"],
        {a: c.confirmed for a, c in by_agent.items()},
    )
    yield _gauge(
        "hivemind_health_success",
        "1 if the last fleet health refresh succeeded, else 0 (the gauges below are then stale)",
        [],
        {"": 1.0 if report.health_status.ok else 0.0},
    )
    if (health := report.health) is not None:
        yield from _health_families(health)


def _optional(name: str, doc: str, value: float | None) -> Iterator[GaugeMetricFamily]:
    """A label-less gauge, absent while ``value`` is unknown."""
    if value is not None:
        yield _gauge(name, doc, [], {"": value})


_HEALTH = "hivemind_health_"


def _health_families(health: HealthUsage) -> Iterator[GaugeMetricFamily]:
    yield from _feedback_families(health)
    yield from _balance_families(health)
    yield from _rival_families(health.rival_lifetime)
    yield from _topic_families(health)
    yield from _fleet_families(health.by_fleet)
    yield _nested_gauge(
        _HEALTH + "role_activity_30d",
        "Last 30 days of writes, writes tagged alternative or dead-end, supersessions written "
        "and verdicts given, for scouts and for everyone else (ADR 0144)",
        ["role", "motion"],
        {role: asdict(activity) for role, activity in health.role_activity_30d.items()},
    )


def _nested_gauge(
    name: str, doc: str, labels: list[str], values: Mapping[str, Mapping[str, float]]
) -> GaugeMetricFamily:
    """A gauge with two labels, from ``{first label: {second label: value}}``."""
    family = GaugeMetricFamily(name, doc, labels=labels)
    for outer, inner in values.items():
        for key, value in inner.items():
            family.add_metric([outer, key], float(value))
    return family


def _feedback_families(health: HealthUsage) -> Iterator[GaugeMetricFamily]:
    """How verdicts are given and acted on, and what is left unchecked."""
    for name, doc, share in (
        (
            "helpful_with_note_ratio",
            "Share of current helpful verdicts that carry a note",
            health.helpful_with_note_share.share,
        ),
        (
            "sourced_ratio",
            "Share of active org- and fleet-scope facts and insights with at least one source",
            health.sourced_share.share,
        ),
        (
            "correction_latency_p50_seconds",
            "Median seconds from an entry's first stale/wrong verdict to its successor, last 90 days",
            health.correction_latency_p50_seconds,
        ),
    ):
        yield from _optional(_HEALTH + name, doc, share)
    for name, doc in (
        ("flagged_backlog_14d", "Active entries flagged stale or wrong more than 14 days ago"),
        (
            "orphaned_flagged",
            "Active flagged entries whose author is revoked or silent for 30 days",
        ),
        ("floating_gap", "Active entries older than 90 days with no feedback in 90 days"),
        ("self_to_fleet_promotions", "Fleet entries superseding a self entry, last 90 days"),
    ):
        yield _gauge(_HEALTH + name, doc, [], {"": getattr(health, name)})


def _balance_families(health: HealthUsage) -> Iterator[GaugeMetricFamily]:
    """Whether a few voices dominate, and whether the hive synthesises rather than echoes."""
    for name, doc, share in (
        (
            "top3_author_write_ratio",
            "Share of the last 90 days' writes made by the three busiest authors",
            health.top3_author_write_share.share,
        ),
        (
            "top3_reporter_verdict_ratio",
            "Share of the last 90 days' verdicts given by the three busiest reporters",
            health.top3_reporter_verdict_share.share,
        ),
        (
            "sibling_verdict_ratio",
            "Share of the last 90 days' verdicts given to an entry by a sibling agent"
            " (same self-reported owner alias, different agent)",
            health.sibling_verdict_share.share,
        ),
        (
            "top3_owner_verdict_ratio",
            "Share of the last 90 days' verdicts given by the three busiest owners"
            " (self-reported owner aliases; no alias is its own owner)",
            health.top3_owner_verdict_share.share,
        ),
        (
            "synthesis_ratio",
            "Share of active insights with at least two see-also links",
            health.synthesis_rate.share,
        ),
        (
            "supersessions_per_100_writes",
            "Supersessions per 100 writes, last 90 days",
            health.supersessions_per_100_writes.per_100,
        ),
        (
            "echo_ratio",
            "Share of the newest sampled writes (30 days) with an earlier entry at cosine >= 0.9",
            health.echo_rate.share,
        ),
        (
            "grounding_ratio",
            "Share of new (30 days) fleet/org insights and decisions with a source that is not an entry",
            health.grounding_share.share,
        ),
        (
            "top3_agent_label_write_ratio",
            "Share of the last 30 days' writes under the three busiest agent labels",
            health.top3_agent_label_write_share.share,
        ),
        (
            "top3_agent_label_verdict_ratio",
            "Share of the last 30 days' verdicts under the three busiest agent labels",
            health.top3_agent_label_verdict_share.share,
        ),
    ):
        yield from _optional(_HEALTH + name, doc, share)


def _rival_families(rival: RivalLifetime) -> Iterator[GaugeMetricFamily]:
    by_kind = {
        "alternative": rival.alternative,
        "conflict": rival.conflict,
        "contested": rival.contested,
    }
    yield _gauge(
        _HEALTH + "rival_entries",
        "Active alternative, Conflict: and contested entries",
        ["kind"],
        {kind: ages.count for kind, ages in by_kind.items()},
    )
    yield _gauge(
        _HEALTH + "rival_median_age_seconds",
        "Median age of those entries (no series while there are none)",
        ["kind"],
        {
            kind: ages.median_age_seconds
            for kind, ages in by_kind.items()
            if ages.median_age_seconds is not None
        },
    )


def _topic_families(health: HealthUsage) -> Iterator[GaugeMetricFamily]:
    # Tags are user data and /metrics has no key: only the mean change leaves the process.
    dispersion = health.topic_dispersion
    yield _gauge(
        _HEALTH + "topic_tags_measured",
        "Busy tags with enough entries in both 30-day windows to measure dispersion",
        [],
        {"": len(dispersion)},
    )
    yield from _optional(
        _HEALTH + "topic_cosine_change",
        "Mean change of the pairwise cosine across the measured tags, last 30 days against the 30 before",
        sum(d.change for d in dispersion) / len(dispersion) if dispersion else None,
    )
    yield _gauge(
        _HEALTH + "gap_entries",
        "Active entries tagged anomaly, open-question or dead-end",
        ["tag"],
        health.gap_tag_counts,
    )
    yield _gauge(
        _HEALTH + "breathing_7d",
        "Writes, verdicts, supersessions and withdrawals of the last 7 days",
        ["motion"],
        asdict(health.breathing_7d),
    )


def _fleet_families(by_fleet: Mapping[str, FleetHealthUsage]) -> Iterator[GaugeMetricFamily]:
    """The per-fleet figures, by the entries' ``fleet_id``."""
    yield _gauge(
        _HEALTH + "fleet_sourced_ratio",
        "Sourced share per fleet (entries' fleet_id)",
        ["fleet"],
        {
            f: r.sourced_share.share
            for f, r in by_fleet.items()
            if r.sourced_share.share is not None
        },
    )
    for name, doc in (
        ("flagged_backlog_14d", "Active entries flagged stale or wrong more than 14 days ago"),
        (
            "orphaned_flagged",
            "Active flagged entries whose author is revoked or silent for 30 days",
        ),
        ("floating_gap", "Active entries older than 90 days with no feedback in 90 days"),
    ):
        yield _gauge(
            _HEALTH + "fleet_" + name,
            doc + ", per fleet",
            ["fleet"],
            {f: getattr(r, name) for f, r in by_fleet.items()},
        )
    yield _gauge(
        _HEALTH + "fleet_active_entries",
        "Active entries per fleet (entries' fleet_id)",
        ["fleet"],
        {f: r.active_entries for f, r in by_fleet.items()},
    )
    yield _nested_gauge(
        _HEALTH + "fleet_breathing_7d",
        "Writes, verdicts, supersessions and withdrawals of the last 7 days per fleet",
        ["fleet", "motion"],
        {f: asdict(r.breathing_7d) for f, r in by_fleet.items()},
    )
    yield _nested_gauge(
        _HEALTH + "fleet_gap_entries",
        "Active gap-tagged entries per fleet and tag",
        ["fleet", "tag"],
        {f: r.gap_tag_counts for f, r in by_fleet.items()},
    )


class PrometheusMetrics:
    """One process's registry, request instrumentation and scrape route."""

    def __init__(
        self,
        usage_report: Callable[[], Awaitable[UsageReport]],
        *,
        ttl: float = USAGE_TTL_SECONDS,
        clock: Clock = time.monotonic,
    ) -> None:
        self._usage_report = usage_report
        self._ttl = ttl
        self._clock = clock
        self._refreshed_at: float | None = None
        self._lock = asyncio.Lock()
        self.usage_snapshot: UsageReport | None = None
        self.usage_ok = False
        self.registry = CollectorRegistry()
        self.process = process = ProcessMetrics(self.registry)
        self.requests = process.requests
        self.latency = process.latency
        self.degraded_searches = process.degraded_searches
        self.registry.register(_UsageCollector(self))

    async def refresh_usage(self) -> None:
        """Refresh the snapshot when older than the TTL; concurrent scrapes wait and reuse it."""
        async with self._lock:
            now = self._clock()
            if self._refreshed_at is not None and now - self._refreshed_at < self._ttl:
                return
            try:
                self.usage_snapshot = await self._usage_report()
                self.usage_ok = True
            except Exception as exc:  # the scrape must still answer
                logger.warning("usage report for /metrics failed: %s", type(exc).__name__)
                self.usage_ok = False
            self._refreshed_at = now

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.get(METRICS_PATH, include_in_schema=False)
        async def metrics() -> Response:
            await self.refresh_usage()
            return Response(generate_latest(self.registry), media_type=CONTENT_TYPE_LATEST)

        return router


__all__ = ["METRICS_PATH", "UNMATCHED_ROUTE", "PrometheusMetrics", "RequestMetrics"]
