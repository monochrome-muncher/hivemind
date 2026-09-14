"""Unit tests for the hostable streamable-HTTP MCP runner (ADR 0010).

Seams under test (all hermetic — no network, no Postgres, no stdio):

* ``build_server(app, credential_provider=...)`` (see ``server.py``): the
  tool dispatch re-binds the *stateless* services to the per-request
  credential, so one process can serve many agents. With no provider, the
  app's own (fixed) credential is used.
* ``BearerAuthMiddleware`` (see ``http.py``): verifies the request's API
  key against the ``Authenticator`` port and stashes the resolved
  ``Credential`` in the per-task context var (so the next dispatch acts as
  that agent), or 401s on a missing / unknown key.

The ASGI pieces are driven in-process with fakes; no real uvicorn / HTTP
loop is started.
"""

from __future__ import annotations

import json
from typing import Any

from hivemind.mcp.app import McpHivemind
from hivemind.mcp.http import (
    BearerAuthMiddleware,
    current_credential,
    make_credential_provider,
)
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.governance import GovernanceService, WriteService
from hivemind.services.search import SearchService
from tests.fakes import make_clock, make_embedder, make_search_config

ALICE = Credential(user_id="alice", agent_id="agent-a")
BOB = Credential(user_id="bob", agent_id="agent-b")
PLACEHOLDER = Credential(user_id="placeholder", agent_id="placeholder-agent")


def make_app(credential: Credential) -> McpHivemind:
    """A minimal ``MemoryStore``-backed ``McpHivemind`` (fake ports)."""
    clock = make_clock()
    store = MemoryStore(clock)
    config = make_search_config()
    return McpHivemind(
        store=store,
        write_service=WriteService(store, make_embedder()),
        search_service=SearchService(store, make_embedder(), config, now_fn=clock),
        governance_service=GovernanceService(store, config),
        search_config=config,
        credential=credential,
    )


class FakeAuthenticator:
    """Dict-backed ``Authenticator`` (the ``Authenticator`` port)."""

    def __init__(self, keys: dict[str, Credential]) -> None:
        self._keys = keys

    async def verify(self, key: str) -> Credential | None:
        return self._keys.get(key)


# --- build_server's per-request credential_provider seam ------------------


async def test_provider_rebinds_credential() -> None:
    """With a provider, each dispatch acts as the provider's credential
    (the shared services, re-bound to it), not the app's placeholder."""
    app = make_app(PLACEHOLDER)
    server = build_server(app, credential_provider=lambda: ALICE)
    result = await server.call_tool("hive_write", {"kind": "fact", "summary": "via provider"})
    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    stored = await app.store.get_entry(payload["id"])
    assert stored is not None
    assert stored.author == ALICE.user_id
    assert stored.agent == ALICE.agent_id


async def test_provider_varying_credential_per_dispatch() -> None:
    """The provider is consulted *per dispatch*, so successive dispatches
    can act as different agents (the shared pool, different provenance)."""
    app = make_app(PLACEHOLDER)
    current: list[Credential] = [ALICE]
    server = build_server(app, credential_provider=lambda: current[0])

    current[0] = ALICE
    res_a = await server.call_tool("hive_write", {"kind": "fact", "summary": "a"})
    current[0] = BOB
    res_b = await server.call_tool("hive_write", {"kind": "fact", "summary": "b"})

    assert res_a.is_error is False and res_b.is_error is False
    entry_a = await app.store.get_entry(json.loads(res_a.content[0].text)["id"])
    entry_b = await app.store.get_entry(json.loads(res_b.content[0].text)["id"])
    assert entry_a is not None and entry_a.agent == ALICE.agent_id
    assert entry_b is not None and entry_b.agent == BOB.agent_id


async def test_no_provider_uses_app_credential() -> None:
    """Without a provider (the stdio / dev path), the fixed ``app`` is used
    and its own credential is what each dispatch acts as."""
    app = make_app(BOB)
    server = build_server(app)
    result = await server.call_tool("hive_write", {"kind": "fact", "summary": "fixed"})
    assert result.is_error is False
    stored = await app.store.get_entry(json.loads(result.content[0].text)["id"])
    assert stored is not None
    assert stored.author == BOB.user_id
    assert stored.agent == BOB.agent_id


# --- BearerAuthMiddleware (the per-request auth seam) --------------------


def _scope(headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {"type": "http", "headers": headers}


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b""}


class _RecordingApp:
    """A minimal inner ASGI app that records the acting credential."""

    def __init__(self) -> None:
        self.cred: Credential | None = None
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        self.cred = current_credential()


async def test_middleware_valid_key_sets_credential_and_dispatches() -> None:
    auth = FakeAuthenticator({"key-alice": ALICE})
    inner = _RecordingApp()
    mw = BearerAuthMiddleware(inner, auth)
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await mw(_scope([(b"authorization", b"Bearer key-alice")]), _noop_receive, send)
    assert inner.called
    assert inner.cred == ALICE
    assert not sent  # the middleware forwards; the inner app owns the response


async def test_middleware_missing_key_401s() -> None:
    auth = FakeAuthenticator({"key-alice": ALICE})
    inner = _RecordingApp()
    mw = BearerAuthMiddleware(inner, auth)
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await mw(_scope([]), _noop_receive, send)
    assert not inner.called
    assert sent and sent[0]["status"] == 401


async def test_middleware_unknown_key_401s() -> None:
    auth = FakeAuthenticator({"key-alice": ALICE})
    inner = _RecordingApp()
    mw = BearerAuthMiddleware(inner, auth)
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await mw(_scope([(b"authorization", b"Bearer key-stranger")]), _noop_receive, send)
    assert not inner.called
    assert sent and sent[0]["status"] == 401


async def test_middleware_non_http_scope_passes_through() -> None:
    """Non-HTTP scopes (e.g. ``lifespan``) bypass auth entirely."""
    auth = FakeAuthenticator({"key-alice": ALICE})
    inner = _RecordingApp()
    mw = BearerAuthMiddleware(inner, auth)

    async def send(msg: dict[str, Any]) -> None:
        pass

    await mw({"type": "lifespan"}, _noop_receive, send)
    assert inner.called
    assert inner.cred is None


# --- make_credential_provider (the per-request credential getter) --------


async def test_credential_provider_is_the_context_var_getter() -> None:
    """``make_credential_provider`` returns the per-task context-var getter:
    unset -> ``None``; set (as the middleware does) -> the set credential."""
    provider = make_credential_provider()
    assert provider() is None
