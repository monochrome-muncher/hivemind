"""The Prometheus series of the streamable-HTTP MCP runner (ADR 0104).

Per-process only: the request counter and histogram and the degraded-search
counter the REST runner serves under the same names, plus the tool-call
counter and histogram. No label carries an agent, a fleet, an owner or a key;
``tool`` is one of the ten registered names. The usage gauges stay on
``hivemind-api``.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client.exposition import generate_latest

from hivemind.http_metrics import ProcessMetrics

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"


class McpMetrics:
    """One MCP process's own registry (never the global default)."""

    content_type = CONTENT_TYPE_LATEST

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.process = ProcessMetrics(self.registry)
        self.tool_calls = Counter(
            "hivemind_mcp_tool_calls",
            "MCP tool calls handled by this process, by tool and outcome (error = an error reply or a raise)",
            ["tool", "outcome"],
            registry=self.registry,
        )
        self.tool_duration = Histogram(
            "hivemind_mcp_tool_duration_seconds",
            "MCP tool call latency in this process",
            ["tool"],
            registry=self.registry,
        )

    def record_tool(self, tool: str, outcome: str, seconds: float) -> None:
        self.tool_calls.labels(tool, outcome).inc()
        self.tool_duration.labels(tool).observe(seconds)

    def degraded_search(self) -> None:
        self.process.degraded_searches.inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)
