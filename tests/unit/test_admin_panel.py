"""The admin panel runner (``hivemind-admin``, ADR 0029).

Pins the proxy allowlist, the headers that do and do not cross, the
security headers, the static bundle's no-inline/no-innerHTML rules, and
that no key ever reaches a log line — end to end through the proxy into
an in-process ``hivemind-api``.
"""

from __future__ import annotations

import logging
import re

import httpx
import pytest

from hivemind.admin.app import CONTENT_SECURITY_POLICY, STATIC_DIR, create_admin_app, is_allowed
from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.memstore import MemoryStore
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config

ADMIN = {"X-API-Key": "hm_admin"}


def _api():
    hivemind = create_app_for_config(
        Settings(),
        store=MemoryStore(make_clock()),
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(),
        search_config=make_search_config(),
    )
    return hivemind, create_app(hivemind)


def _panel(transport: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    app = create_admin_app("http://hivemind-api.test", transport=transport)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://admin.test")


# -- the allowlist -----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "health"),
        ("GET", "metrics"),
        ("GET", "admin/agents"),
        ("GET", "admin/fleets"),
        ("POST", "admin/fleets"),
        ("GET", "admin/audit-log"),
        ("POST", "admin/agents/alice/activate"),
        ("POST", "admin/agents/alice/revoke"),
        ("PATCH", "admin/agents/alice"),
        ("POST", "admin/org-key/rotate"),
    ],
)
def test_the_admin_surface_is_forwarded(method: str, path: str) -> None:
    assert is_allowed(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "entries"),
        ("POST", "entries"),
        ("POST", "search"),
        ("POST", "agents"),  # registration is org-key self-service, not admin
        ("POST", "entries/x/withdraw"),
        ("GET", "admin/agents/alice/activate"),  # right path, wrong method
        ("DELETE", "admin/agents/alice"),
        ("PATCH", "admin/agents/alice/extra"),
        ("GET", "admin/agents/../../entries"),
        ("GET", "admin/audit-logx"),
    ],
)
def test_everything_else_is_refused(method: str, path: str) -> None:
    assert not is_allowed(method, path)


async def test_a_refused_call_never_reaches_the_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    async with _panel(httpx.MockTransport(handler)) as panel:
        resp = await panel.post("/v1/entries", json={"body": "x"}, headers=ADMIN)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_proxied"
    assert seen == []


# -- the upstream URL is built from re-quoted segments (ADR 0040, ADMIN-1) ---------


@pytest.mark.parametrize(
    ("incoming", "upstream_raw_path"),
    [
        ("/v1/admin/agents/bob%23", b"/v1/admin/agents/bob%23"),
        ("/v1/admin/agents/x%3Fy", b"/v1/admin/agents/x%3Fy"),
        ("/v1/admin/agents/a%2541", b"/v1/admin/agents/a%2541"),
        ("/v1/admin/agents/a%20b", b"/v1/admin/agents/a%20b"),
        ("/v1/admin/agents/a.b", b"/v1/admin/agents/a.b"),
    ],
)
async def test_a_name_with_reserved_url_characters_reaches_the_same_resource(
    incoming: str, upstream_raw_path: bytes
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async with _panel(httpx.MockTransport(handler)) as panel:
        resp = await panel.patch(incoming, json={"trust_level": 1}, headers=ADMIN)
    assert resp.status_code == 200
    [request] = seen
    assert request.url.raw_path == upstream_raw_path
    assert request.url.query == b""


@pytest.mark.parametrize("segment", ["%2e%2e", "%2E%2E", "%2e", "."])
async def test_dot_segments_are_refused_not_collapsed(segment: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async with _panel(httpx.MockTransport(handler)) as panel:
        revoke = await panel.post(f"/v1/admin/agents/{segment}/revoke", headers=ADMIN)
        patch = await panel.patch(f"/v1/admin/agents/{segment}", json={}, headers=ADMIN)
    assert revoke.status_code == patch.status_code == 404
    assert seen == []


async def test_patching_bob_hash_through_the_panel_does_not_touch_bob() -> None:
    """The reported bug: PATCH on ``bob%23`` mutated ``bob``."""
    hivemind, api_app = _api()
    fleet = await hivemind.store.create_fleet("f")
    for name in ("bob", "bob#"):  # a legacy name: registration now refuses '#'
        await hivemind.store.register_agent(name, None)
        await hivemind.store.activate_agent(
            name, trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id
        )
    async with _panel(httpx.ASGITransport(app=api_app)) as panel:
        resp = await panel.patch("/v1/admin/agents/bob%23", json={"trust_level": 3}, headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["name"] == "bob#"
    assert (await hivemind.store.get_agent("bob")).trust_level is TrustLevel.LURKER
    assert (await hivemind.store.get_agent("bob#")).trust_level is TrustLevel.PRIVILEGED


# -- what crosses the proxy ----------------------------------------------------


async def test_only_the_key_and_content_headers_cross() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    async with _panel(httpx.MockTransport(handler)) as panel:
        await panel.get(
            "/v1/admin/audit-log?action=agent.revoke&limit=5",
            headers={**ADMIN, "Cookie": "session=abc", "Authorization": "Bearer x", "X-Other": "y"},
        )
    [request] = seen
    assert request.url.path == "/v1/admin/audit-log"
    assert dict(request.url.params) == {"action": "agent.revoke", "limit": "5"}
    assert request.headers["x-api-key"] == "hm_admin"
    for dropped in ("cookie", "authorization", "x-other"):
        assert dropped not in request.headers


async def test_an_unreachable_api_is_a_502_in_the_api_error_shape(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom hm_admin", request=request)

    caplog.set_level(logging.DEBUG)
    async with _panel(httpx.MockTransport(handler)) as panel:
        resp = await panel.get("/v1/admin/agents", headers=ADMIN)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "api_unreachable"
    assert "hm_admin" not in caplog.text


# -- end to end into a real API app ----------------------------------------------


async def test_activation_through_the_proxy_returns_the_key_uncached_and_unlogged(
    caplog,
) -> None:
    hivemind, api_app = _api()
    await hivemind.store.register_agent("alice", "john")
    fleet = await hivemind.store.create_fleet("data-eng")
    caplog.set_level(logging.DEBUG)
    async with _panel(httpx.ASGITransport(app=api_app)) as panel:
        pending = await panel.get("/v1/admin/agents", headers=ADMIN)
        activated = await panel.post(
            "/v1/admin/agents/alice/activate",
            json={"trust_level": 2, "home_fleet_id": fleet.id},
            headers=ADMIN,
        )
        denied = await panel.get("/v1/admin/agents", headers={"X-API-Key": "hm_org"})
    assert [a["status"] for a in pending.json()] == ["pending"]
    assert activated.status_code == 200
    raw_key = activated.json()["key"]
    assert activated.headers["cache-control"] == "no-store"
    assert denied.status_code == 403  # the API still does the authorization
    assert raw_key not in caplog.text
    assert "hm_admin" not in caplog.text


# -- the static bundle and its headers ----------------------------------------------


async def test_every_response_carries_the_security_headers() -> None:
    _, api_app = _api()
    async with _panel(httpx.ASGITransport(app=api_app)) as panel:
        responses = [
            await panel.get("/"),
            await panel.get("/static/admin.js"),
            await panel.get("/static/admin.css"),
            await panel.get("/healthz"),
            await panel.get("/v1/health"),
        ]
    for resp in responses:
        assert resp.status_code == 200, resp.url
        assert resp.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["referrer-policy"] == "no-referrer"


def test_the_csp_allows_only_same_origin_scripts_and_connections() -> None:
    directives = dict(d.strip().split(" ", 1) for d in CONTENT_SECURITY_POLICY.split(";"))
    assert directives["script-src"] == "'self'"
    assert directives["connect-src"] == "'self'"
    assert directives["default-src"] == "'none'"
    assert "unsafe" not in CONTENT_SECURITY_POLICY


def test_the_page_has_no_inline_script_or_style() -> None:
    html = (STATIC_DIR / "index.html").read_text()
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html) == []
    assert re.search(r"\sstyle=", html) is None
    assert re.search(r"\son[a-z]+=", html) is None
    assert "<style" not in html


def test_the_script_never_writes_html() -> None:
    js = (STATIC_DIR / "admin.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in js, sink
    # Loaded from nowhere but the panel itself.
    assert re.search(r"https?://", js) is None


def test_the_runner_refuses_to_start_without_an_api_url(monkeypatch) -> None:
    from hivemind.admin import main

    monkeypatch.delenv("HIVEMIND_ADMIN_API_URL", raising=False)
    monkeypatch.setattr(main, "uvicorn", None)  # would crash if reached
    with pytest.raises(SystemExit, match="HIVEMIND_ADMIN_API_URL"):
        main.run()
