"""Tests for the unauthenticated orchestrator probe endpoints (ADR 0019).

Seams under test (all hermetic — no network, no Postgres):

* ``ProbeRouter`` (``mcp/http.py``): a thin ASGI router that serves
  ``/mcp/liveness`` (shallow 200) and ``/mcp/health`` (deep: 200 iff
  the ``Store.health_check`` answers, else 503) OUTSIDE the auth
  middleware — probes carry no credential (k8s probes have no key).
* The wiring in ``build_http_app``: the probe paths are answered
  without any API key, while the MCP transport surface (``/mcp``)
  still 401s without one.

Driven at the ASGI level (like ``test_mcp_http.py``); no uvicorn.
"""

from __future__ import annotations

import json
from typing import Any

from hivemind.config import Settings
from hivemind.mcp.http import ProbeRouter, build_http_app
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from tests.fakes import make_embedder
from tests.unit.test_mcp_http import FakeAuthenticator


class _RecordingApp:
    """A stub ASGI app: records the scopes it is called with."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.seen.append(scope)


class UnhealthyStore(MemoryStore):
    """A store whose pool answers nothing (the 503 posture)."""

    async def health_check(self) -> bool:
        return False


def _probe_router(store: MemoryStore, inner: _RecordingApp | None = None) -> ProbeRouter:
    return ProbeRouter(inner if inner is not None else _RecordingApp(), store)


def _http_scope(path: str, method: str = "GET") -> dict[str, Any]:
    return {"type": "http", "method": method, "path": path, "headers": [(b"host", b"test")]}


def _lifespan_scope() -> dict[str, Any]:
    return {"type": "lifespan"}


async def _drive(app: Any, scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Drive one ASGI request with no input; return the raw send events."""

    async def receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


def _status_and_body(sent: list[dict[str, Any]]) -> tuple[int, dict[str, Any]]:
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], json.loads(body or b"{}")


# --- ProbeRouter (the unauthenticated surface) ------------------------------


async def test_liveness_is_shallow_and_unauthenticated() -> None:
    inner = _RecordingApp()
    sent = await _drive(_probe_router(MemoryStore(), inner), _http_scope("/mcp/liveness"))
    status, body = _status_and_body(sent)
    assert status == 200
    assert body == {"status": "ok"}
    assert not inner.seen  # the probe never reaches the inner app


async def test_health_is_deep_when_the_store_is_healthy() -> None:
    inner = _RecordingApp()
    sent = await _drive(_probe_router(MemoryStore(), inner), _http_scope("/mcp/health"))
    status, body = _status_and_body(sent)
    assert status == 200
    assert body == {"status": "ok"}
    assert not inner.seen


async def test_health_is_503_when_the_store_is_unhealthy() -> None:
    inner = _RecordingApp()
    sent = await _drive(_probe_router(UnhealthyStore(), inner), _http_scope("/mcp/health"))
    status, body = _status_and_body(sent)
    assert status == 503
    assert body == {"status": "unhealthy", "detail": "database unreachable"}
    assert not inner.seen


async def test_non_probe_paths_are_delegated_to_the_inner_app() -> None:
    inner = _RecordingApp()
    scope = _http_scope("/mcp")
    await _drive(_probe_router(MemoryStore(), inner), scope)
    assert scope in inner.seen  # delegated untouched


async def test_non_get_methods_on_probe_paths_are_delegated() -> None:
    inner = _RecordingApp()
    scope = _http_scope("/mcp/liveness", method="POST")
    await _drive(_probe_router(MemoryStore(), inner), scope)
    assert scope in inner.seen  # probes are GET-only; anything else passes through


async def test_non_http_scopes_pass_through() -> None:
    inner = _RecordingApp()
    scope = _lifespan_scope()
    await _drive(_probe_router(MemoryStore(), inner), scope)
    assert scope in inner.seen


# --- build_http_app wiring (probes live OUTSIDE the auth middleware) ---------


def _make_authenticator() -> FakeAuthenticator:
    return FakeAuthenticator({"key-alice": Credential(user_id="alice", agent_id="agent-a")})


async def test_build_http_app_serves_probes_without_a_key() -> None:
    import httpx

    inner_app = build_http_app(Settings(), MemoryStore(), make_embedder(), _make_authenticator())
    transport = httpx.ASGITransport(app=inner_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe.test") as client:
        liveness = await client.get("/mcp/liveness")
        assert liveness.status_code == 200
        assert liveness.json() == {"status": "ok"}
        health = await client.get("/mcp/health")
        assert health.status_code == 200  # MemoryStore is always healthy
        # The MCP transport surface is still protected: no key -> 401.
        assert (await client.get("/mcp")).status_code == 401


async def test_build_http_app_health_reflects_the_store() -> None:
    import httpx

    inner_app = build_http_app(Settings(), UnhealthyStore(), make_embedder(), _make_authenticator())
    transport = httpx.ASGITransport(app=inner_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://probe.test") as client:
        health = await client.get("/mcp/health")
        assert health.status_code == 503
        assert health.json() == {"status": "unhealthy", "detail": "database unreachable"}
