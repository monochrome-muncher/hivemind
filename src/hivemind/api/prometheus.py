"""The Prometheus scrape endpoint of the REST runner (ADR 0050).

``GET /metrics`` answers in the Prometheus text format, unauthenticated
like every scrape target, so it must stay inside the cluster: the
optional Ingress sends it to the source-allowlisted ``hivemind-admin-api``
Ingress (deny by default), never to the public one.

Three groups of series:

* **Usage gauges** from ``MetricsService.usage_report`` (the same counts
  ``GET /v1/metrics`` returns). The whole report is cached here for
  ``USAGE_TTL_SECONDS`` and refreshed by at most one scrape at a time.
  Its expensive part, the grouped scan of the entries, is cached again
  inside ``MetricsService`` for ``HIVEMIND_METRICS_ENTRY_COUNTS_TTL``
  (default 300 s), so the entry gauges can lag by up to that long; the
  agent, fleet and search gauges are as fresh as this layer's TTL. Every
  replica reports the same pool, so aggregate them with ``max``, not
  ``sum``.
* **HTTP request metrics** of this process: a counter and a latency
  histogram per method and route *template* (``/v1/entries/{entry_id}``,
  never the raw path, so the label set stays bounded).
* **Degraded searches** (ADR 0048): searches answered by the keyword
  stream alone because the embedder failed.
* **Search counters** (ADR 0056): first-page searches and the ones that
  found nothing or were explore searches (ADR 0078), per fleet, read from
  the pool with the usage report, so they cover every runner (MCP
  included). Aggregate them with ``max``.
* **Flag confirmation** (ADR 0068): `hivemind_feedback_flags` and
  `hivemind_feedback_flags_confirmed` per registered agent, the
  stale/wrong reports and the ones a later supersession confirmed. An
  observation, never a ranking input; agents with no flags have no series.

* **Fleet health** (ADR 0075): the `hivemind_health_*` gauges, admin-facing
  observations of the pool (verdict notes, sources, correction speed,
  orphaned flags, concentration, synthesis, gap tags) with a few per-fleet
  series. Never a ranking input. Cached with the entry scan; a share with
  no denominator has no series.

A failed usage refresh keeps the last snapshot and sets
``hivemind_usage_report_success`` to 0; the scrape itself still answers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.exposition import generate_latest
from prometheus_client.registry import Collector
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hivemind.domain.usage import HealthUsage, UsageReport

logger = logging.getLogger(__name__)

METRICS_PATH = "/metrics"
USAGE_TTL_SECONDS = 30.0
UNMATCHED_ROUTE = "unmatched"

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


def _health_families(health: HealthUsage) -> Iterator[GaugeMetricFamily]:
    """The fleet health gauges (ADR 0075)."""
    p = "hivemind_health_"
    yield from _optional(
        p + "helpful_with_note_ratio",
        "Share of current helpful verdicts that carry a note",
        health.helpful_with_note_share.share,
    )
    yield from _optional(
        p + "sourced_ratio",
        "Share of active org- and fleet-scope facts and insights with at least one source",
        health.sourced_share.share,
    )
    yield from _optional(
        p + "correction_latency_p50_seconds",
        "Median seconds from an entry's first stale/wrong verdict to its successor, last 90 days",
        health.correction_latency_p50_seconds,
    )
    for name, doc in (
        ("flagged_backlog_14d", "Active entries flagged stale or wrong more than 14 days ago"),
        (
            "orphaned_flagged",
            "Active flagged entries whose author is revoked or silent for 30 days",
        ),
        ("floating_gap", "Active entries older than 90 days with no feedback in 90 days"),
        ("self_to_fleet_promotions", "Fleet entries superseding a self entry, last 90 days"),
    ):
        yield _gauge(p + name, doc, [], {"": getattr(health, name)})
    yield from _optional(
        p + "top3_author_write_ratio",
        "Share of the last 90 days' writes made by the three busiest authors",
        health.top3_author_write_share.share,
    )
    yield from _optional(
        p + "top3_reporter_verdict_ratio",
        "Share of the last 90 days' verdicts given by the three busiest reporters",
        health.top3_reporter_verdict_share.share,
    )
    yield from _optional(
        p + "sibling_verdict_ratio",
        "Share of the last 90 days' verdicts given to an entry by a sibling agent"
        " (same self-reported owner alias, different agent)",
        health.sibling_verdict_share.share,
    )
    yield from _optional(
        p + "top3_owner_verdict_ratio",
        "Share of the last 90 days' verdicts given by the three busiest owners"
        " (self-reported owner aliases; no alias is its own owner)",
        health.top3_owner_verdict_share.share,
    )
    yield from _optional(
        p + "synthesis_ratio",
        "Share of active insights with at least two see-also links",
        health.synthesis_rate.share,
    )
    yield from _optional(
        p + "supersessions_per_100_writes",
        "Supersessions per 100 writes, last 90 days",
        health.supersessions_per_100_writes.per_100,
    )
    yield from _optional(
        p + "echo_ratio",
        "Share of the newest sampled writes (30 days) with an earlier entry at cosine >= 0.9",
        health.echo_rate.share,
    )
    yield from _optional(
        p + "grounding_ratio",
        "Share of new (30 days) fleet/org insights and decisions with a source that is not an entry",
        health.grounding_share.share,
    )
    yield from _optional(
        p + "top3_agent_label_write_ratio",
        "Share of the last 30 days' writes under the three busiest agent labels",
        health.top3_agent_label_write_share.share,
    )
    yield from _optional(
        p + "top3_agent_label_verdict_ratio",
        "Share of the last 30 days' verdicts under the three busiest agent labels",
        health.top3_agent_label_verdict_share.share,
    )
    rival = health.rival_lifetime
    yield _gauge(
        p + "rival_entries",
        "Active alternative, Conflict: and contested entries",
        ["kind"],
        {
            "alternative": rival.alternative.count,
            "conflict": rival.conflict.count,
            "contested": rival.contested.count,
        },
    )
    yield _gauge(
        p + "rival_median_age_seconds",
        "Median age of those entries (no series while there are none)",
        ["kind"],
        {
            kind: ages.median_age_seconds
            for kind, ages in (
                ("alternative", rival.alternative),
                ("conflict", rival.conflict),
                ("contested", rival.contested),
            )
            if ages.median_age_seconds is not None
        },
    )
    # Tag strings are user data and /metrics has no key: only the mean change
    # across the measured tags leaves the process (the tags are in the admin JSON).
    dispersion = health.topic_dispersion
    yield _gauge(
        p + "topic_tags_measured",
        "Busy tags with enough entries in both 30-day windows to measure dispersion",
        [],
        {"": len(dispersion)},
    )
    yield from _optional(
        p + "topic_cosine_change",
        "Mean change of the pairwise cosine across the measured tags, last 30 days against the 30 before",
        sum(d.change for d in dispersion) / len(dispersion) if dispersion else None,
    )
    yield _gauge(
        p + "gap_entries",
        "Active entries tagged anomaly, open-question or dead-end",
        ["tag"],
        health.gap_tag_counts,
    )
    by_fleet = health.by_fleet
    sourced = GaugeMetricFamily(
        p + "fleet_sourced_ratio", "Sourced share per fleet (entries' fleet_id)", labels=["fleet"]
    )
    for fleet, row in by_fleet.items():
        if row.sourced_share.share is not None:
            sourced.add_metric([fleet], float(row.sourced_share.share))
    yield sourced
    for name, doc in (
        ("flagged_backlog_14d", "Active entries flagged stale or wrong more than 14 days ago"),
        (
            "orphaned_flagged",
            "Active flagged entries whose author is revoked or silent for 30 days",
        ),
        ("floating_gap", "Active entries older than 90 days with no feedback in 90 days"),
    ):
        yield _gauge(
            p + "fleet_" + name,
            doc + ", per fleet",
            ["fleet"],
            {f: getattr(r, name) for f, r in by_fleet.items()},
        )
    gap = GaugeMetricFamily(
        p + "fleet_gap_entries",
        "Active gap-tagged entries per fleet and tag",
        labels=["fleet", "tag"],
    )
    for fleet, row in by_fleet.items():
        for tag, count in row.gap_tag_counts.items():
            gap.add_metric([fleet, tag], float(count))
    yield gap


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
        self.requests = Counter(
            "hivemind_http_requests",
            "HTTP requests handled by this process",
            ["method", "route", "status"],
            registry=self.registry,
        )
        self.latency = Histogram(
            "hivemind_http_request_duration_seconds",
            "HTTP request latency in this process",
            ["method", "route"],
            registry=self.registry,
        )
        self.degraded_searches = Counter(
            "hivemind_search_degraded",
            "Searches answered by the keyword stream alone because the embedder failed (ADR 0048)",
            registry=self.registry,
        )
        self.registry.register(_UsageCollector(self))

    async def refresh_usage(self) -> None:
        """Refresh the usage snapshot when it is older than the TTL. One
        refresh at a time; concurrent scrapes wait and reuse its result."""
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
            """Prometheus scrape target (ADR 0050). Unauthenticated: keep it
            off the public Ingress."""
            await self.refresh_usage()
            return Response(generate_latest(self.registry), media_type=CONTENT_TYPE_LATEST)

        return router


class RequestMetrics:
    """Pure-ASGI middleware: count and time every HTTP request by route
    template. Outermost, so 401/413 answers from the guards are counted."""

    def __init__(self, app: ASGIApp, metrics: PrometheusMetrics) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self._app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            template = getattr(route, "path_format", None) or getattr(route, "path", None)
            label = template if isinstance(template, str) else UNMATCHED_ROUTE
            method = str(scope.get("method", ""))
            self._metrics.requests.labels(method, label, str(status)).inc()
            self._metrics.latency.labels(method, label).observe(time.perf_counter() - started)
