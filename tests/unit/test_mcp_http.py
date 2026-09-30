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
from dataclasses import replace
from typing import Any

import pytest

from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryFilters
from hivemind.mcp.app import McpHivemind
from hivemind.mcp.http import (
    BearerAuthMiddleware,
    build_http_app,
    build_transport_security,
    current_credential,
    make_credential_provider,
)
from hivemind.mcp.server import build_server
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
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
        access_service=AccessService(store),
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


# --- ADR 0042: fail closed, WWW-Authenticate, no OAuth discovery ----------


async def test_provider_returning_none_fails_closed() -> None:
    """MCP-5 / AUTH-7a: a provider that yields no credential must NOT fall
    back to the app's own (template) credential: every verb answers
    ``unauthenticated`` and nothing is read or written."""
    app = make_app(Credential(user_id="shared", agent_id="template"))
    server = build_server(app, credential_provider=lambda: None)
    for tool, args in (
        ("hive_write", {"kind": "fact", "summary": "x"}),
        ("hive_list", {}),
        ("hive_whoami", {}),
    ):
        result = await server.call_tool(tool, args)
        assert json.loads(result.content[0].text)["error"]["code"] == "unauthenticated", tool
    assert await app.store.list_entries(EntryFilters()) == []


async def test_async_provider_is_awaited_per_dispatch() -> None:
    app = make_app(PLACEHOLDER)
    calls: list[int] = []

    async def provider() -> Credential | None:
        calls.append(1)
        return ALICE

    server = build_server(app, credential_provider=provider)
    await server.call_tool("hive_whoami", {})
    await server.call_tool("hive_whoami", {})
    assert len(calls) == 2


async def test_401_carries_www_authenticate_bearer() -> None:
    mw = BearerAuthMiddleware(_RecordingApp(), FakeAuthenticator({}))
    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    await mw(_scope([]), _noop_receive, send)
    headers = dict(sent[0]["headers"])
    assert sent[0]["status"] == 401
    assert headers[b"www-authenticate"] == b'Bearer realm="hivemind"'


async def test_oauth_discovery_paths_answer_404_without_touching_the_authenticator() -> None:
    """ONB-8: there is no OAuth; clients probing ``/.well-known/oauth-*``
    must see 404, not a 401 that starts an OAuth flow."""

    class ExplodingAuth(FakeAuthenticator):
        async def verify(self, key: str) -> Credential | None:
            raise AssertionError("discovery must not reach the authenticator")

    inner = _RecordingApp()
    mw = BearerAuthMiddleware(inner, ExplodingAuth({}))
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server",
    ):
        sent: list[dict[str, Any]] = []

        async def send(msg: dict[str, Any], sent: list[dict[str, Any]] = sent) -> None:
            sent.append(msg)

        await mw({"type": "http", "path": path, "headers": []}, _noop_receive, send)
        assert sent[0]["status"] == 404, path
    assert not inner.called


# --- MCP-9: transport security (DNS-rebinding) setting --------------------


def test_transport_security_default_keeps_sdk_behaviour() -> None:
    assert build_transport_security(Settings(), "0.0.0.0") is None


def test_transport_security_hosts_and_origins_are_wired() -> None:
    ts = build_transport_security(
        Settings(
            mcp_allowed_hosts="hive.example.com, hive:*",
            mcp_allowed_origins="https://app.example.com",
        ),
        "0.0.0.0",
    )
    assert ts is not None
    assert ts.enable_dns_rebinding_protection is True
    assert ts.allowed_hosts == ["hive.example.com", "hive:*"]
    assert ts.allowed_origins == ["https://app.example.com"]


def test_transport_security_origins_without_hosts_is_an_error() -> None:
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        build_transport_security(Settings(mcp_allowed_origins="https://x"), "0.0.0.0")


def test_built_app_rejects_a_foreign_host_when_hosts_are_set() -> None:
    """End to end through ``build_http_app`` (lifespan running): a wrong
    Host answers 421, the allowed Host gets past the transport check."""
    from starlette.testclient import TestClient

    app = build_http_app(
        Settings(mcp_allowed_hosts="hive.example.com"),
        MemoryStore(),
        make_embedder(),
        FakeAuthenticator({"k": ALICE}),
    )
    headers = {
        "authorization": "Bearer k",
        "content-type": "application/json",
        "accept": "application/json, text/event-stream",
    }
    with TestClient(app) as client:
        bad = client.post("/mcp", content=b"{}", headers={**headers, "host": "evil.example.com"})
        good = client.post("/mcp", content=b"{}", headers={**headers, "host": "hive.example.com"})
    assert bad.status_code == 421
    assert good.status_code != 421


# --- ADR 0042: the stdio runner re-verifies per call ----------------------


async def test_reverified_credential_revocation_and_demotion_apply_next_call() -> None:
    """MCP-1 / AUTH-7b: with a provider that re-verifies the key (as
    ``main_pg`` wires it), revoking the key refuses the very next call and
    a trust demotion changes what the next call may do."""
    contributor = Credential(
        user_id="alice",
        agent_id="alice",
        agent_name="alice",
        access_controlled=True,
        trust_level=TrustLevel.CONTRIBUTOR,
        home_fleet_id="fleet-1",
    )
    keys: dict[str, Credential] = {"key-alice": contributor}
    auth = FakeAuthenticator(keys)

    async def reverify() -> Credential | None:
        return await auth.verify("key-alice")

    server = build_server(make_app(PLACEHOLDER), credential_provider=reverify)

    ok = await server.call_tool("hive_write", {"kind": "fact", "summary": "one"})
    assert "id" in json.loads(ok.content[0].text)

    keys["key-alice"] = replace(contributor, trust_level=TrustLevel.UNTRUSTED)
    denied = await server.call_tool("hive_write", {"kind": "fact", "summary": "two"})
    assert "error" in json.loads(denied.content[0].text)

    del keys["key-alice"]
    gone = await server.call_tool("hive_whoami", {})
    assert json.loads(gone.content[0].text)["error"]["code"] == "unauthenticated"
