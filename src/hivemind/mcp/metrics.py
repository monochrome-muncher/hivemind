"""The Prometheus series of the streamable-HTTP MCP runner (ADR 0104).

Per-process only: the request counter and histogram and the degraded-search
counter the REST runner serves under the same names, plus the tool-call
counter and histogram. No label carries an agent, a fleet, an owner or a key;
``tool`` is one of the ten registered names and ``code`` one of the error codes
in ``mcp/errors.py`` (ADR 0161). The usage gauges stay on ``hivemind-api``.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram
from prometheus_client.exposition import generate_latest

from hivemind.http_metrics import ProcessMetrics
from hivemind.mcp.errors import ERR_EMBEDDING_UNAVAILABLE, ERR_STORE_UNAVAILABLE, ERROR_CODES

OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
CODE_NONE = "none"
CODE_RAISED = "raised"
CODE_OTHER = "other"
# The codes the dashboard counts as server-side failures; every other code is a refusal the caller
# can act on.
FAILURE_CODES = frozenset({ERR_STORE_UNAVAILABLE, ERR_EMBEDDING_UNAVAILABLE, CODE_RAISED})


def error_code_label(result: dict[str, object]) -> str | None:
    """The reply's error code as a bounded label value, or ``None`` for a reply that is no error."""
    error = result.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if code in ERROR_CODES else CODE_OTHER


class McpMetrics:
    """One MCP process's own registry (never the global default)."""

    content_type = CONTENT_TYPE_LATEST

    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.process = ProcessMetrics(self.registry)
        self.tool_calls = Counter(
            "hivemind_mcp_tool_calls",
            "MCP tool calls handled by this process, by tool, outcome (error = an error reply or a "
            "raise) and error code (none, raised, or the reply's code)",
            ["tool", "outcome", "code"],
            registry=self.registry,
        )
        self.tool_duration = Histogram(
            "hivemind_mcp_tool_duration_seconds",
            "MCP tool call latency in this process",
            ["tool"],
            registry=self.registry,
        )

    def record_tool(self, tool: str, code: str | None, seconds: float) -> None:
        """Count one call: ``code`` is ``None`` for a success, else its label value."""
        outcome = OUTCOME_OK if code is None else OUTCOME_ERROR
        self.tool_calls.labels(tool, outcome, code or CODE_NONE).inc()
        self.tool_duration.labels(tool).observe(seconds)

    def degraded_search(self) -> None:
        self.process.degraded_searches.inc()

    def render(self) -> bytes:
        return generate_latest(self.registry)
