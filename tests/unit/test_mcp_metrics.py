"""The MCP runner's per-process Prometheus series (ADR 0104).

Seams: ``GET /metrics`` on ``build_http_app`` (opt-in; served before the
auth middleware, request counting outermost), the degraded-search hook on
``McpHivemind`` and the tool counters bound by ``build_server``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families
from starlette.testclient import TestClient

from hivemind.config import Settings
from hivemind.embeddings import EmbeddingError
from hivemind.mcp.http import build_http_app
from hivemind.mcp.metrics import McpMetrics
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from tests.fakes import make_embedder
from tests.unit.test_mcp_http import ALICE, FakeAuthenticator, make_app

Samples = dict[tuple[str, tuple[tuple[str, str], ...]], float]


def samples(text: str) -> Samples:
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


KEYS = {"key-alice": Credential(user_id="alice", agent_id="agent-a")}
MCP_HEADERS = {
    "authorization": "Bearer key-alice",
    "content-type": "application/json",
    "accept": "application/json, text/event-stream",
}


def _app(
    *,
    enabled: bool = True,
    embedder: Any = None,
    authenticator: Any = None,
) -> Any:
    return build_http_app(
        Settings(mcp_metrics_enabled=enabled),
        MemoryStore(),
        embedder or make_embedder(),
        authenticator or FakeAuthenticator(KEYS),
    )


def _client(**kwargs: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=_app(**kwargs))
    return httpx.AsyncClient(transport=transport, base_url="http://mcp.test")


def _call_tool(client: Any, name: str, arguments: dict[str, Any]) -> Any:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    return client.post("/mcp", json=body, headers=MCP_HEADERS)


async def test_metrics_is_served_without_a_key() -> None:
    async with _client() as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


async def test_a_keyless_mcp_request_is_401_and_counted_by_route() -> None:
    async with _client() as client:
        assert (await client.post("/mcp", json={})).status_code == 401
        await client.get("/mcp/liveness")
        got = samples((await client.get("/metrics")).text)
    key = (
        "hivemind_http_requests_total",
        (("method", "POST"), ("route", "/mcp"), ("status", "401")),
    )
    assert got[key] == 1
    probe = (
        "hivemind_http_requests_total",
        (("method", "GET"), ("route", "/mcp/liveness"), ("status", "200")),
    )
    assert got[probe] == 1
    duration = (
        "hivemind_http_request_duration_seconds_count",
        (("method", "POST"), ("route", "/mcp")),
    )
    assert got[duration] == 1


async def test_an_unknown_path_is_one_bounded_label() -> None:
    async with _client() as client:
        await client.get("/a/secret-path-1")
        await client.get("/a/secret-path-2")
        text = (await client.get("/metrics")).text
    got = samples(text)
    key = (
        "hivemind_http_requests_total",
        (("method", "GET"), ("route", "unmatched"), ("status", "401")),
    )
    assert got[key] == 2
    assert "secret-path" not in text


async def test_no_usage_series_are_served() -> None:
    async with _client() as client:
        text = (await client.get("/metrics")).text
    for name in (
        "hivemind_entries",
        "hivemind_health_",
        "hivemind_searches_total",
        "hivemind_agents",
    ):
        assert name not in text


async def test_post_and_a_trailing_slash_are_not_the_metrics_path() -> None:
    async with _client() as client:
        assert (await client.post("/metrics")).status_code == 401
        assert (await client.get("/metrics/")).status_code == 401


async def test_metrics_is_a_404_before_auth_when_disabled() -> None:
    async with _client(enabled=False) as client:
        assert (await client.get("/metrics")).status_code == 404
        assert (await client.post("/mcp", json={})).status_code == 401


def test_metrics_are_off_by_default() -> None:
    assert Settings().mcp_metrics_enabled is False


def test_a_keyed_tool_call_is_counted_as_a_request_and_a_tool_call() -> None:
    with TestClient(_app()) as client:
        response = _call_tool(client, "hive_whoami", {})
        assert response.status_code == 200
        got = samples(client.get("/metrics").text)
    request = (
        "hivemind_http_requests_total",
        (("method", "POST"), ("route", "/mcp"), ("status", "200")),
    )
    assert got[request] == 1
    tool = ("hivemind_mcp_tool_calls_total", (("outcome", "ok"), ("tool", "hive_whoami")))
    assert got[tool] == 1


def test_a_degraded_search_through_the_app_is_counted() -> None:
    class DownEmbedder:
        dimension = 4
        model_name = "down"

        async def embed_text(self, text: str) -> list[float]:
            raise EmbeddingError("embedding endpoint is down")

        async def embed_entry(self, draft: Any) -> list[float]:
            raise EmbeddingError("embedding endpoint is down")

    with TestClient(_app(embedder=DownEmbedder())) as client:
        for _ in range(2):
            response = _call_tool(client, "hive_search", {"query": "gamma"})
            assert response.status_code == 200
            assert "keyword_only" in response.text
        got = samples(client.get("/metrics").text)
    assert got[("hivemind_search_degraded_total", ())] == 2


async def test_an_authenticator_timeout_is_503_and_counted() -> None:
    class SlowAuth(FakeAuthenticator):
        async def verify(self, key: str) -> Credential | None:
            raise TimeoutError

    async with _client(authenticator=SlowAuth({})) as client:
        response = await client.post("/mcp", json={}, headers=MCP_HEADERS)
        got = samples((await client.get("/metrics")).text)
    assert response.status_code == 503
    key = (
        "hivemind_http_requests_total",
        (("method", "POST"), ("route", "/mcp"), ("status", "503")),
    )
    assert got[key] == 1


async def test_an_odd_method_is_labelled_other() -> None:
    async with _client() as client:
        await client.request("PROPFIND", "/mcp")
        await client.request("X-" + "A" * 20, "/mcp")
        got = samples((await client.get("/metrics")).text)
    key = (
        "hivemind_http_requests_total",
        (("method", "other"), ("route", "/mcp"), ("status", "401")),
    )
    assert got[key] == 2


async def test_tool_calls_are_counted_by_outcome() -> None:
    metrics = McpMetrics()
    server = build_server(make_app(ALICE), metrics=metrics)
    await server.call_tool("hive_whoami", {})
    await server.call_tool("hive_get", {"id": "no-such-entry"})
    got = samples(metrics.render().decode())
    assert got[("hivemind_mcp_tool_calls_total", (("outcome", "ok"), ("tool", "hive_whoami")))] == 1
    assert got[("hivemind_mcp_tool_calls_total", (("outcome", "error"), ("tool", "hive_get")))] == 1
    assert got[("hivemind_mcp_tool_duration_seconds_count", (("tool", "hive_whoami"),))] == 1


async def test_a_raising_tool_is_counted_as_an_error_and_reraised() -> None:
    metrics = McpMetrics()
    app = make_app(ALICE)

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("boom")

    app.store.list_entries = broken  # type: ignore[method-assign]
    server = build_server(app, metrics=metrics)
    with pytest.raises(Exception, match="hive_list"):
        await server.call_tool("hive_list", {})
    got = samples(metrics.render().decode())
    assert (
        got[("hivemind_mcp_tool_calls_total", (("outcome", "error"), ("tool", "hive_list")))] == 1
    )


async def test_without_metrics_the_server_works_unchanged() -> None:
    server = build_server(make_app(ALICE))
    result = await server.call_tool("hive_whoami", {})
    assert "trust_level" in json.loads(result.content[0].text)
