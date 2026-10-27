"""The admin panel runner (``hivemind-admin``, ADR 0029).

Pins the proxy allowlist, the headers that do and do not cross, the
security headers, the static bundle's no-inline/no-innerHTML rules, and
that no key ever reaches a log line — end to end through the proxy into
an in-process ``hivemind-api``.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from hivemind.admin import app as admin_module
from hivemind.admin.app import CONTENT_SECURITY_POLICY, STATIC_DIR, create_admin_app, is_allowed
from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.memstore import MemoryStore
from hivemind.portal import app as portal_module
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
        ("GET", "admin/entries"),
        ("GET", "admin/entries/abc123"),
        ("POST", "admin/entries/abc123/withdraw"),
        ("POST", "admin/search"),
        ("GET", "admin/activity"),
        ("GET", "admin/feedback"),
        ("GET", "admin/fleets/abc/pins"),
        ("GET", "admin/flagged"),
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
        ("GET", "admin/entries/x/withdraw"),  # right path, wrong method
        ("DELETE", "admin/entries/x"),
        ("PATCH", "admin/entries/x"),
        ("POST", "admin/entries"),
        ("PUT", "admin/entries/x/withdraw"),
        ("GET", "admin/search"),
        ("POST", "admin/activity"),
        ("POST", "admin/feedback"),
        ("GET", "admin/feedback/x"),
        ("POST", "admin/fleets/abc/pins"),
        ("GET", "admin/fleets/abc/pins/x"),
        ("GET", "admin/fleets//pins"),
        ("POST", "admin/flagged"),
        ("GET", "admin/flagged/x"),
        ("GET", "admin/entries/x/extra"),
        ("GET", "admin/entries/"),
    ],
)
def test_everything_else_is_refused(method: str, path: str) -> None:
    assert not is_allowed(method, path)


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


# -- the allowlists name real API routes ----------------------------------------------


def _api_routes_from(spec: dict) -> set[tuple[str, str]]:
    """The API's (METHOD, path after /v1/) pairs, ``{id}`` as the allowlist's ``[^/]+``."""
    return {
        (method.upper(), re.sub(r"\{[^}]+\}", "[^/]+", path.removeprefix("/v1/")))
        for path, operations in spec["paths"].items()
        if path.startswith("/v1/")
        for method in operations
    }


@pytest.mark.parametrize("front_end", ["admin", "portal"])
async def test_every_allowlisted_call_names_a_real_api_route(front_end: str) -> None:
    """A renamed API route would otherwise turn into a silent 404 at the proxy."""
    allowed = {"admin": admin_module._ALLOWED, "portal": portal_module._ALLOWED}[front_end]
    _, api_app = _api()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://api.test"
    ) as api:
        routes = _api_routes_from((await api.get("/openapi.json")).json())
    assert allowed
    for method, pattern in allowed:
        assert (method, pattern.pattern) in routes, (method, pattern.pattern)


STATIC_TRAVERSALS = [
    "/static/../webapp.py",
    "/static/%2e%2e/webapp.py",
    "/static/%2E%2E/webapp.py",
    "/static/..%2Fwebapp.py",
    "/static/%2e%2e%2fwebapp.py",
    "/static/..%2F..%2Fapi%2Fdeps.py",
    "/static/%2e%2e/admin/app.py",
    "/static//etc/passwd",
]


@pytest.mark.parametrize("front_end", ["admin", "portal"])
@pytest.mark.parametrize("path", STATIC_TRAVERSALS)
async def test_static_never_serves_a_file_outside_its_folder(front_end: str, path: str) -> None:
    create = {"admin": create_admin_app, "portal": portal_module.create_portal_app}[front_end]
    app = create("http://hivemind-api.test")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://panel.test"
    ) as panel:
        resp = await panel.get(path)
    assert resp.status_code in (400, 404)
    assert "import " not in resp.text and "root:" not in resp.text


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
    assert "hivemind-api unreachable: ConnectError" in caplog.text  # something was logged
    assert "hm_admin" not in caplog.text


# -- end to end into a real API app ----------------------------------------------


async def test_activation_through_the_proxy_returns_the_key_uncached_and_unlogged(
    caplog,
) -> None:
    hivemind, api_app = _api()
    await hivemind.store.register_agent("alice", "john")
    fleet = await hivemind.store.create_fleet("data-eng")
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.INFO, logger="httpx")  # other tests may have quieted it
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
    assert "/v1/admin/agents/alice/activate" in caplog.text  # the proxied call was logged
    assert raw_key not in caplog.text
    assert "hm_admin" not in caplog.text


# -- the static bundle and its headers ----------------------------------------------


def test_the_csp_allows_only_same_origin_scripts_and_connections() -> None:
    directives = dict(d.strip().split(" ", 1) for d in CONTENT_SECURITY_POLICY.split(";"))
    assert directives["script-src"] == "'self'"
    assert directives["connect-src"] == "'self'"
    assert directives["default-src"] == "'none'"
    assert "unsafe" not in CONTENT_SECURITY_POLICY


def _scripts() -> list[Path]:
    scripts = sorted(STATIC_DIR.glob("*.js"))
    assert scripts, "no panel script found"
    return scripts


def test_the_scripts_load_only_sibling_modules() -> None:
    names = {p.name for p in _scripts()}
    for script in _scripts():
        for target in re.findall(r"""from\s+["']([^"']+)["']""", script.read_text()):
            assert target.startswith("./"), (script.name, target)
            assert target[2:] in names, (script.name, target)


def test_the_scripts_never_build_a_link_from_entry_data() -> None:
    """Entry text is data (ADR 0043): a link is an in-panel hash and nothing else."""
    for script in _scripts():
        js = script.read_text()
        if script.name == "csv.js":
            # The one exception: the download link points at a Blob URL the
            # module itself made from the CSV text, never at entry data.
            assert "const url = URL.createObjectURL(new Blob(" in js
            js = js.replace("a.href = url;", "", 1)
        assert ".href =" not in js and 'setAttribute("href"' not in js, script.name
        for href in re.findall(r"\bhref:\s*([^,}]+)", js):
            assert re.search(r"\b(?:memories|feedback|agent|view)Href\(", href) or re.match(
                r"""\s*(?:["'`]#|href\b|null)""", href
            ), (script.name, href)
    # The href helpers build in-panel hash routes and nothing else.
    ui = (STATIC_DIR / "ui.js").read_text()
    assert "return `#/${view}" in ui
    for helper in ("memoriesHref", "feedbackHref", "agentHref"):
        assert re.search(rf"export const {helper} = \([^)]*\) => viewHref\(", ui), helper


# -- the pre-auth guard (ADR 0040), as on the API --------------------------------


async def test_a_keyless_call_is_refused_before_its_body_is_read() -> None:
    seen: list[httpx.Request] = []
    pulled: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    async def body():  # type: ignore[no-untyped-def]
        for _ in range(4):
            pulled.append(1)  # the app asked for another chunk
            yield b"x" * 1024

    async with _panel(httpx.MockTransport(handler)) as panel:
        resp = await panel.post("/v1/admin/fleets", content=body())
        health = await panel.get("/v1/health")
    assert resp.status_code == 401
    assert pulled == []
    assert resp.json()["error"]["code"] == "missing_api_key"
    # The public probe still needs no key.
    assert health.status_code == 200
    assert [r.url.path for r in seen] == ["/v1/health"]


@pytest.mark.parametrize("chunked", [False, True])
async def test_an_oversized_body_is_refused_and_never_forwarded(chunked: bool) -> None:
    from hivemind.domain.validation import MAX_REQUEST_BODY_BYTES

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={})

    big = b"x" * (MAX_REQUEST_BODY_BYTES + 1)

    async def stream():
        for i in range(0, len(big), 65536):
            yield big[i : i + 65536]

    async with _panel(httpx.MockTransport(handler)) as panel:
        resp = await panel.post(
            "/v1/admin/fleets", content=stream() if chunked else big, headers=ADMIN
        )
        ok = await panel.post("/v1/admin/fleets", json={"name": "f"}, headers=ADMIN)
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "payload_too_large"
    assert ok.status_code == 201
    assert len(seen) == 1


def test_the_shared_modules_import_nothing_admin_specific() -> None:
    """ADR 0091: these can be served to another front end as they are."""
    shared = {"ui.js", "client.js", "lists.js", "drawer.js", "charts.js", "csv.js"}
    for name in shared:
        js = (STATIC_DIR / name).read_text()
        imports = {t[2:] for t in re.findall(r"""from\s+["']([^"']+)["']""", js)}
        assert imports <= shared, (name, imports - shared)
        assert "admin/" not in js and "hivemind.adminKey" not in js, name


def test_views_fill_their_root_without_printing_null() -> None:
    """``replaceChildren(a, null)`` prints "null"; views use ui.js ``fill``."""
    for script in _scripts():
        assert "root.replaceChildren(" not in script.read_text(), script.name


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_csv_export_quotes_cells_defuses_formulas_and_pages_to_the_end() -> None:
    """The CSV module (csv.js) run as it ships: RFC 4180 quoting, no
    spreadsheet formula from agent text, every page fetched up to the cap."""
    script = r"""
    const csv = await import(process.argv[1])
    const columns = [csv.column("summary"), csv.column("tags"), csv.column("n"), csv.column("none")]
    const text = csv.toCsv(columns, [
      { summary: 'say "hi", then\nleave', tags: ["a", "b"], n: -3 },
      { summary: "=HYPERLINK(1)", tags: ["@x"], n: 0 },
      { summary: "+1", tags: [], n: 1.5 },
      { summary: " =1+1", tags: ["\uFF1Dx"], n: true },
    ])
    const pages = (total) => async ({ limit, offset }) =>
      Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, i) => offset + i)
    const exact = await csv.fetchAll(pages(250))
    const capped = await csv.fetchAll(pages(500), { max: 200 })
    const full = await csv.fetchAll(pages(200))
    const stopped = await csv.fetchAll(pages(500), { cancelled: () => true })
    const signedOut = await csv.fetchAll(async () => undefined)
    const exactMax = await csv.fetchAll(pages(200), { max: 200 })
    // A row written mid-export shifts the second page back by one.
    const shifted = async ({ limit, offset }) =>
      offset >= 200 ? [] : Array.from({ length: limit }, (_, i) => offset - (offset ? 1 : 0) + i)
    const deduped = await csv.fetchAll(shifted, { key: (r) => r })
    console.log(JSON.stringify({
      text,
      exact: [exact.rows.length, exact.truncated, exact.rows[249]],
      capped: [capped.rows.length, capped.truncated],
      full: [full.rows.length, full.truncated],
      stopped, signedOut,
      exactMax: [exactMax.rows.length, exactMax.truncated],
      deduped: [deduped.rows.length, new Set(deduped.rows).size],
      name: csv.csvName("agent-a/b c", new Date("2026-10-18T12:00:00Z")),
    }))
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, (STATIC_DIR / "csv.js").as_uri()],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(out.stdout)
    assert result["text"] == (
        "summary,tags,n,none\r\n"
        '"say ""hi"", then\nleave",a; b,-3,\r\n'
        "'=HYPERLINK(1),'@x,0,\r\n"
        "'+1,,1.5,\r\n"
        "' =1+1,'\uff1dx,true,\r\n"
    )
    assert result["exact"] == [250, False, 249]
    assert result["capped"] == [200, True]
    assert result["full"] == [200, False]  # a full last page costs one empty read
    assert result["exactMax"] == [200, False]  # exactly the cap: a probe says nothing is cut
    assert result["deduped"] == [199, 199]
    assert result["stopped"] is None and result["signedOut"] is None
    assert result["name"] == "hivemind-agent-a_b_c-2026-10-18.csv"
