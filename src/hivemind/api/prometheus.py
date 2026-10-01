"""The Prometheus scrape endpoint of the REST runner (ADR 0050).

``GET /metrics`` answers in the Prometheus text format, unauthenticated
like every scrape target, so it must stay inside the cluster: the
optional Ingress sends it to the source-allowlisted ``hivemind-admin-api``
Ingress (deny by default), never to the public one.

Three groups of series:

* **Usage gauges** from ``MetricsService.usage_report`` (the same counts
  ``GET /v1/metrics`` returns). The report is one grouped scan of the
  pool, so it is cached for ``USAGE_TTL_SECONDS`` and refreshed by at
  most one scrape at a time. Every replica reports the same pool, so
  aggregate them with ``max``, not ``sum``.
* **HTTP request metrics** of this process: a counter and a latency
  histogram per method and route *template* (``/v1/entries/{entry_id}``,
  never the raw path, so the label set stays bounded).
* **Degraded searches** (ADR 0048): searches answered by the keyword
  stream alone because the embedder failed.

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
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.exposition import generate_latest
from prometheus_client.registry import Collector
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

METRICS_PATH = "/metrics"
USAGE_TTL_SECONDS = 30.0
UNMATCHED_ROUTE = "unmatched"

UsageReport = Mapping[str, Mapping[str, Any]]
Clock = Callable[[], float]


class _UsageCollector(Collector):
    """Yields the cached usage snapshot as gauges (read at scrape time)."""

    def __init__(self, owner: PrometheusMetrics) -> None:
        self._owner = owner

    def collect(self) -> Iterator[GaugeMetricFamily]:
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


def _usage_families(report: UsageReport) -> Iterator[GaugeMetricFamily]:
    entries, fleets, agents = report["entries"], report["fleets"], report["agents"]
    yield _gauge(
        "hivemind_entries",
        "Entries in the pool, by state (inactive = superseded or withdrawn)",
        ["state"],
        {"active": entries["active"], "inactive": entries["inactive"]},
    )
    yield _gauge(
        "hivemind_entries_by_scope", "Entries by scope (all states)", ["scope"], entries["by_scope"]
    )
    yield _gauge(
        "hivemind_entries_by_kind", "Entries by kind (all states)", ["kind"], entries["by_kind"]
    )
    yield _gauge(
        "hivemind_entries_by_importance_source",
        "Entries by whether importance was caller-supplied or the default",
        ["importance_source"],
        entries["by_importance_source"],
    )
    by_author_kind = GaugeMetricFamily(
        "hivemind_entries_by_author_kind",
        "Entries per registered agent and kind (all states)",
        labels=["author", "kind"],
    )
    for author, kinds in entries["by_author_kind"].items():
        for kind, count in kinds.items():
            by_author_kind.add_metric([author, kind], float(count))
    yield by_author_kind
    fleet_total = GaugeMetricFamily("hivemind_fleets", "Fleets")
    fleet_total.add_metric([], float(fleets["total"]))
    yield fleet_total
    yield _gauge(
        "hivemind_fleet_entries",
        "Entries written into each fleet (all states)",
        ["fleet"],
        fleets["writes_by_fleet"],
    )
    yield _gauge(
        "hivemind_agents",
        "Registered agents, by status",
        ["status"],
        {s: agents[s] for s in ("pending", "active", "revoked")},
    )
    yield _gauge(
        "hivemind_agents_by_trust_level",
        "Registered agents, by trust level (all statuses)",
        ["trust_level"],
        agents["by_trust_level"],
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
