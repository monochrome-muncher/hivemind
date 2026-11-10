"""Per-process request series both HTTP runners serve under the same names, so a dashboard sums the
``api`` and ``mcp`` jobs (ADR 0050, 0104). No label names an agent, a fleet or a key.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from prometheus_client import CollectorRegistry, Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

UNMATCHED_ROUTE = "unmatched"

RouteLabel = Callable[[Scope], str]


class ProcessMetrics:
    """Request counter, latency histogram and degraded-search counter, registered in
    ``registry``."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.requests = Counter(
            "hivemind_http_requests",
            "HTTP requests handled by this process",
            ["method", "route", "status"],
            registry=registry,
        )
        self.latency = Histogram(
            "hivemind_http_request_duration_seconds",
            "HTTP request latency in this process",
            ["method", "route"],
            registry=registry,
        )
        self.degraded_searches = Counter(
            "hivemind_search_degraded",
            "Searches answered by the keyword stream alone because the embedder failed (ADR 0048)",
            registry=registry,
        )


def route_template(scope: Scope) -> str:
    """The matched route template (``/v1/entries/{entry_id}``), never the raw path (bounded labels);
    ``unmatched`` for a 404."""
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template if isinstance(template, str) else UNMATCHED_ROUTE


_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _method_label(scope: Scope) -> str:
    """The method if standard, else ``other``: it is client-chosen, so raw values would be a
    cardinality attack."""
    method = str(scope.get("method", ""))
    return method if method in _METHODS else "other"


class RequestMetrics:
    """Pure-ASGI middleware counting and timing every request. Outermost, so guard 401/413s are
    counted."""

    def __init__(
        self, app: ASGIApp, metrics: ProcessMetrics, route_label: RouteLabel = route_template
    ) -> None:
        self._app = app
        self._metrics = metrics
        self._route_label = route_label

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
            label = self._route_label(scope)
            method = _method_label(scope)
            self._metrics.requests.labels(method, label, str(status)).inc()
            self._metrics.latency.labels(method, label).observe(time.perf_counter() - started)
