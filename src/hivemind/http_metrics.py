"""The per-process request series both HTTP runners serve (ADR 0050, 0104).

The REST runner and the streamable-HTTP MCP runner each keep their own
``CollectorRegistry`` and count the same three series here, under the same
names, labels and buckets, so a dashboard sums them across the ``api`` and
``mcp`` jobs. Nothing here names an agent, a fleet or a key.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from prometheus_client import CollectorRegistry, Counter, Histogram
from starlette.types import ASGIApp, Message, Receive, Scope, Send

UNMATCHED_ROUTE = "unmatched"

RouteLabel = Callable[[Scope], str]


class ProcessMetrics:
    """The request counter, the latency histogram and the degraded-search
    counter of this process, registered in ``registry``."""

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
    """The matched route's template (``/v1/entries/{entry_id}``), never the
    raw path, so the label set stays bounded; ``unmatched`` for a 404."""
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return template if isinstance(template, str) else UNMATCHED_ROUTE


_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def _method_label(scope: Scope) -> str:
    """The request method when it is a standard one, else ``other``: the
    method is client-chosen, so an unbounded label would be a cardinality
    attack on an unauthenticated scrape target."""
    method = str(scope.get("method", ""))
    return method if method in _METHODS else "other"


class RequestMetrics:
    """Pure-ASGI middleware: count and time every HTTP request by the label
    ``route_label`` gives it. Outermost, so 401/413 answers from the guards
    are counted."""

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
