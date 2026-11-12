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
        ("POST", "admin/agents"),
        ("GET", "admin/fleets"),
        ("POST", "admin/fleets"),
        ("GET", "admin/audit-log"),
        ("POST", "admin/agents/alice/activate"),
        ("POST", "admin/agents/alice/revoke"),
        ("POST", "admin/agents/alice/key"),
        ("PATCH", "admin/agents/alice"),
        ("POST", "admin/org-key/rotate"),
        ("GET", "admin/entries"),
        ("GET", "admin/entries/abc123"),
        ("POST", "admin/entries/abc123/withdraw"),
        ("POST", "admin/entries/abc123/restore"),
        ("POST", "admin/search"),
        ("GET", "admin/activity"),
        ("GET", "admin/feedback"),
        ("GET", "admin/fleets/abc/pins"),
        ("GET", "admin/flagged"),
        ("POST", "admin/fleets/abc/merge"),
        ("PATCH", "admin/fleets/abc"),
        ("POST", "admin/fleets/abc/move"),
        ("GET", "admin/fleet-merges"),
        ("POST", "admin/fleet-merges/abc/undo"),
        ("POST", "admin/agents/alice/retire"),
        ("POST", "admin/owners/jane.doe/offboard"),
        ("POST", "admin/owners/ops/jane/offboard"),  # an alias may hold a slash
        ("GET", "admin/successions"),
        ("POST", "admin/successions/alice/undo"),
        ("POST", "admin/agents/bob/cleanup"),
        ("GET", "admin/agent-cleanups"),
        ("POST", "admin/agent-cleanups/abc/undo"),
        ("POST", "admin/entries/abc/redact"),
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
        ("GET", "portal/history"),  # the portal's own route, never the admin proxy's
        ("GET", "admin/entries/x/withdraw"),  # right path, wrong method
        ("DELETE", "admin/entries/x"),
        ("PATCH", "admin/entries/x"),
        ("POST", "admin/entries"),
        ("PUT", "admin/entries/x/withdraw"),
        ("GET", "admin/entries/x/restore"),  # right path, wrong method
        ("POST", "admin/entries//restore"),
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
        ("GET", "admin/fleets/abc/merge"),  # right path, wrong method
        ("POST", "admin/fleets//merge"),
        ("GET", "admin/fleets/abc/move"),  # right path, wrong method
        ("POST", "admin/fleets//move"),
        ("POST", "admin/fleets/abc/move/x"),
        ("POST", "admin/fleets/abc/merge/x"),
        ("POST", "admin/fleet-merges"),
        ("GET", "admin/fleet-merges/abc"),
        ("GET", "admin/fleet-merges/abc/undo"),
        ("DELETE", "admin/fleet-merges/abc"),
        ("POST", "admin/fleet-merges//undo"),
        ("GET", "admin/agents/alice/retire"),  # right path, wrong method
        ("POST", "admin/agents//retire"),
        ("POST", "admin/owners//offboard"),
        ("POST", "admin/owners/jane/offboard/x"),
        ("GET", "admin/owners/jane/offboard"),
        ("GET", "admin/owners"),
        ("POST", "admin/agents/alice/retire/x"),
        ("POST", "admin/successions"),
        ("GET", "admin/successions/alice"),
        ("GET", "admin/successions/alice/undo"),
        ("DELETE", "admin/successions/alice"),
        ("POST", "admin/successions//undo"),
        ("GET", "admin/agents/bob/cleanup"),  # right path, wrong method
        ("POST", "admin/agents//cleanup"),
        ("POST", "admin/agents/bob/cleanup/x"),
        ("POST", "admin/agent-cleanups"),
        ("GET", "admin/agent-cleanups/abc"),
        ("GET", "admin/agent-cleanups/abc/undo"),
        ("DELETE", "admin/agent-cleanups/abc"),
        ("POST", "admin/agent-cleanups//undo"),
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
        # ``[^/].*`` is a ``{name:path}`` parameter, which the OpenAPI document shows as ``{name}``.
        path = pattern.pattern.replace("[^/].*", "[^/]+")
        assert (method, path) in routes, (method, pattern.pattern)


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
            assert re.search(
                r"\b(?:memories|feedback|agent|owner|fleet|view)Href\(", href
            ) or re.match(r"""\s*(?:["'`]#|href\b|null)""", href), (script.name, href)
    # The href helpers build in-panel hash routes and nothing else.
    ui = (STATIC_DIR / "ui.js").read_text()
    assert "return `#/${view}" in ui
    for helper in ("memoriesHref", "feedbackHref", "agentHref", "ownerHref", "fleetHref"):
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


def test_the_fleet_page_is_routed_and_reads_only_allowlisted_fleet_routes() -> None:
    admin = (STATIC_DIR / "admin.js").read_text()
    assert 'import { viewFleet } from "./fleet.js";' in admin
    assert "fleet: viewFleet" in admin
    assert 'fleet: "fleets"' in admin  # the Fleets nav item stays lit
    page = (STATIC_DIR / "fleet.js").read_text()
    routes = set(re.findall(r'api\("GET", ["`](admin/[a-z-]+(?:/\$\{[^}]+\}/pins)?)', page))
    assert routes == {
        "admin/entries",
        "admin/flagged",
        "admin/audit-log",
        "admin/fleets",
        "admin/fleet-merges",
        "admin/fleets/${encodeURIComponent(id)}/pins",
    }
    for name in ("agent.js", "owner.js", "manage.js", "stats.js"):
        assert "fleetHref(" in (STATIC_DIR / name).read_text(), name


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_fleet_page_picks_its_members_and_merges() -> None:
    script = r"""
    const { recordsForFleet, membersOf } = await import(process.argv[1])
    const merges = [
      { id: "1", source_fleet_id: "a", target_fleet_id: "b" },
      { id: "2", source_fleet_id: "c", target_fleet_id: "a" },
      { id: "3", source_fleet_id: "c", target_fleet_id: "b" },
    ]
    const agents = [{ name: "zed", home_fleet_id: "a" }, { name: "amy", home_fleet_id: "a" }, { name: "bob", home_fleet_id: "b" }]
    console.log(JSON.stringify({
      merges: recordsForFleet(merges, "a").map((m) => m.id), members: membersOf(agents, "a").map((a) => a.name),
    }))
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, (STATIC_DIR / "fleetdata.js").as_uri()],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    got = json.loads(out.stdout)
    assert got["merges"] == ["1", "2"]
    assert got["members"] == ["amy", "zed"]


def _since(script: str) -> object:
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, (STATIC_DIR / "since.js").as_uri()],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_since_keeps_the_visit_and_falls_back_when_storage_is_blocked() -> None:
    """since.js run as it ships: the last visit is read once, a refresh keeps it, a blocked store falls back."""
    got = _since(r"""
    const m = await import(process.argv[1])
    const store = () => { const d = {}; return { getItem: (k) => (k in d ? d[k] : null), setItem: (k, v) => { d[k] = String(v) } } }
    const blocked = { getItem() { throw new Error("no") }, setItem() { throw new Error("no") } }
    const t1 = new Date("2026-11-10T10:00:00Z"), t2 = new Date("2026-11-11T12:00:00Z"), t3 = new Date("2026-11-12T09:00:00Z")
    const local = store(), session = store()
    const first = m.beginVisit("k", { now: t1, local, session })
    const unloaded = m.beginVisit("k", { now: t1, local, session }) // nothing stored until commit
    first.commit()
    const refresh = m.beginVisit("k", { now: t2, local, session }) // same tab: keeps the first visit's since
    m.resetVisits()
    const newTab = m.beginVisit("k", { now: t3, local, session: store() }) // new tab: the stored last visit
    m.resetVisits()
    const noStore = m.beginVisit("k", { now: t2, local: blocked, session: blocked })
    noStore.commit()
    const again = m.beginVisit("k", { now: t3, local: blocked, session: blocked })
    console.log(JSON.stringify({
      first: [first.since, first.fallback], unloaded: unloaded.fallback, refresh: [refresh.since, refresh.fallback],
      newTab: [newTab.since, newTab.fallback], noStore: noStore.fallback, again: again.since,
      junk: m.resolveSince("nonsense", t1), future: m.resolveSince("2030-01-01T00:00:00Z", t1).fallback,
    }))
    """)
    assert isinstance(got, dict)
    assert got["first"] == ["2026-11-03T10:00:00.000Z", True]
    assert got["unloaded"] is True
    assert got["refresh"] == ["2026-11-03T10:00:00.000Z", True]
    assert got["newTab"] == ["2026-11-10T10:00:00.000Z", False]
    assert got["noStore"] is True
    assert got["again"] == "2026-11-04T12:00:00.000Z"  # in-memory visit, seven days before t2
    assert got["junk"] == {"since": "2026-11-03T10:00:00.000Z", "fallback": True}
    assert got["future"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_since_counts_and_words_the_line() -> None:
    got = _since(r"""
    const m = await import(process.argv[1])
    const since = "2026-11-11T14:02:00.000Z"
    const agents = [
      { name: "a", created_at: "2026-11-11T15:00:00Z", key_locked_at: null },
      { name: "b", created_at: "2026-11-01T15:00:00Z", key_locked_at: "2026-11-12T00:00:00Z" },
      { name: "c", created_at: "2026-11-12T15:00:00Z", key_locked_at: null },
    ]
    const entries = Array.from({ length: 100 }, (_, i) => ({ id: String(i) }))
    const flagged = [{ first_flag_at: "2026-11-01T00:00:00Z" }, { first_flag_at: "2026-11-12T00:00:00Z" }]
    const audit = [
      ...Array.from({ length: 4 }, () => ({ action: "entry.withdraw" })),
      { action: "agent.activate" }, { action: "agent.register" }, { action: "agent_key.lock" },
      ...Array.from({ length: 9 }, () => ({ action: "fleet.rename" })),
      // An agent's own pin or cleanup is not an admin action.
      { action: "entry.pin", actor_kind: "agent_key" }, { action: "agent.cleanup", actor_kind: "agent_key" },
    ]
    const parts = m.adminParts({ since, agents, entries, flagged, audit })
    const failed = m.adminParts({ since, agents, entries: null, flagged: undefined, audit: null })
    const none = m.adminParts({ since, agents: [], entries: [], flagged: [], audit: [] })
    const visit = { since, fallback: false }
    console.log(JSON.stringify({
      parts: parts.map((p) => p.text), failed: failed.map((p) => p.text), none,
      head: m.sinceHead(visit, parts, "UTC"), nothing: m.sinceHead(visit, none, "UTC"),
      fallback: m.sinceHead({ since, fallback: true }, parts), label: m.sinceLabel(since, "UTC"),
      one: m.adminParts({ since, entries: [{}] }).map((p) => p.text),
      pins: m.portalParts({ since, memories: 3, pins: [{ pinned_at: "2026-11-12T00:00:00Z" }, { pinned_at: "2026-01-01T00:00:00Z" }] }).map((p) => p.text),
      full: m.portalParts({ since, memories: 100, memoriesFull: true }).map((p) => p.text),
    }))
    """)
    assert isinstance(got, dict)
    assert got["parts"] == [
        "100+ new memories", "2 new agents", "1 key locked", "1 new flag", "4 withdrawals", "9 other admin actions",
    ]  # fmt: skip
    assert got["failed"] == ["2 new agents", "1 key locked"]
    assert got["none"] == []
    assert got["head"] == "Since Wed 11 Nov, 14:02: "
    assert got["nothing"] == "Nothing new since Wed 11 Nov, 14:02."
    assert got["fallback"] == "In the last 7 days: "
    assert got["label"] == "Wed 11 Nov, 14:02"
    assert got["one"] == ["1 new memory"]
    assert got["pins"] == ["3 new memories", "1 new pin"]
    assert got["full"] == ["100+ new memories"]


def test_the_overview_reads_each_part_on_its_own_and_stores_the_visit_once_loaded() -> None:
    js = (STATIC_DIR / "overview.js").read_text()
    assert "hivemind.admin.lastSeen" in js
    assert js.count("visit.commit()") == 1 and js.index("visit.commit()") > js.index("fill(root")
    assert js.count("optional(") >= 4  # entries, flagged and audit reads each optional


def test_the_shared_modules_import_nothing_admin_specific() -> None:
    """ADR 0091: these can be served to another front end as they are."""
    shared = {
        "ui.js",
        "client.js",
        "lists.js",
        "drawer.js",
        "charts.js",
        "csv.js",
        "explain.js",
        "memfilters.js",
        "since.js",
    }
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_needs_you_row_lists_only_what_is_above_zero_locked_keys_first() -> None:
    """ADR 0121: needs.js run as it ships, on the data the Overview loaded."""
    script = r"""
    const { needsYouItems } = await import(process.argv[1])
    const agent = (o) => ({ status: "active", key_locked: false, idle: false, idle_after_days: 14, ...o })
    const quiet = needsYouItems([agent({}), agent({ status: "revoked", key_locked: true })], { health: { flagged_backlog_14d: 0 } })
    const noMetrics = needsYouItems([], null)
    const busy = needsYouItems([
      agent({ idle: true }), agent({ key_locked: true }), agent({ status: "pending" }), agent({ status: "pending" }),
    ], { health: { flagged_backlog_14d: 3 } })
    console.log(JSON.stringify({ quiet, noMetrics, busy: busy.map((i) => [i.id, i.count, i.href, Boolean(i.urgent), i.hint]) }))
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, (STATIC_DIR / "needs.js").as_uri()],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    got = json.loads(out.stdout)
    assert got["quiet"] == [] and got["noMetrics"] == []
    assert got["busy"] == [
        ["locked", 1, "#/agents?activity=locked", True, "A leak was reported"],
        ["pending", 2, "#/agents?status=pending", False, "Waiting for activation"],
        ["idle", 1, "#/agents?activity=idle", False, "No request for 14+ days"],
        ["flags", 3, "#/health?flags=old", False, "Flagged over 14 days ago, still active"],
    ]


def test_the_agents_page_and_health_page_read_the_filters_the_needs_row_links_to() -> None:
    manage = (STATIC_DIR / "manage.js").read_text()
    assert 'params.get("status")' in manage and 'params.get("activity")' in manage
    health = (STATIC_DIR / "health.js").read_text()
    # The tile counts what the Health row counts, and its link is that row's filter.
    assert "flagged_backlog_14d" in health and "older_than_days: 14" in health
    assert "flagged_backlog_14d" in (STATIC_DIR / "needs.js").read_text()
    assert "needsYou(state.agents, metrics)" in (STATIC_DIR / "overview.js").read_text()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_memory_id_in_the_search_box_reads_that_memory() -> None:
    """ui.js run as it ships: only a whole memory id (any case, spaces
    around it) counts as an id, and a 404 from the read is no rows, never
    an error, so a missing id and an unreadable one look the same."""
    script = r"""
    const ui = await import(process.argv[1])
    const id = "3f2b9c1e-8a4d-4e6f-9b0a-1c2d3e4f5a6b"
    const ids = [id, ` ${id.toUpperCase()} `, id.slice(0, 8), `${id} kaniko`, "kaniko", "", null]
      .map((t) => ui.entryIdIn(t))
    const notFound = Object.assign(new Error("unknown entry"), { status: 404 })
    const boom = Object.assign(new Error("down"), { status: 503 })
    const found = await ui.entryById(async () => ({ id }))
    const missing = await ui.entryById(async () => { throw notFound })
    const signedOut = await ui.entryById(async () => undefined)
    let other = null
    try { await ui.entryById(async () => { throw boom }) } catch (err) { other = err.status }
    console.log(JSON.stringify({ ids, found, missing, signedOut: signedOut === undefined, other }))
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, (STATIC_DIR / "ui.js").as_uri()],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    got = json.loads(out.stdout)
    entry_id = "3f2b9c1e-8a4d-4e6f-9b0a-1c2d3e4f5a6b"
    assert got["ids"] == [entry_id, entry_id, None, None, None, None, None]
    assert got["found"] == [{"id": entry_id}]
    assert got["missing"] == []
    assert got["signedOut"] is True
    assert got["other"] == 503


def test_the_memory_browser_reads_an_id_through_the_admin_entry_read() -> None:
    """An id in the search box goes to ``GET admin/entries/{id}``, the
    drawer's own read, not to the search."""
    script = (STATIC_DIR / "memories.js").read_text()
    assert "entryIdIn(f.q)" in script
    assert 'api("GET", `admin/entries/${encodeURIComponent(id)}`)' in script


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_search_as_an_agent_says_why_each_hidden_memory_is_hidden() -> None:
    """ADR 0136: asagent.js run as it ships: the banner, the reason for each
    kind of hidden hit, and only the admin's hits the agent did not get."""
    script = r"""
    const { asAgentBanner, hiddenReason, hiddenFrom, ownedAuthors } = await import(process.argv[1])
    const fleets = { f1: "blue", f2: "red" }
    const name = (id) => fleets[id]
    const amy = { name: "amy", trust_level: 2, home_fleet_id: "f1" }
    const hit = (o) => ({ id: "e", author: "bob", scope: "fleet", fleet_id: "f1", state: "active", ...o })
    const reasons = [
      hit({ scope: "self" }),
      hit({ scope: "fleet", fleet_id: "f2" }),
      hit({ scope: "fleet", fleet_id: "f2", author: "amy" }),
      hit({ state: "withdrawn" }),
      hit({}),
    ].map((h) => hiddenReason(amy, h, { fleetName: name }))
    const privileged = hiddenReason({ ...amy, trust_level: 3 }, hit({ fleet_id: "f2" }), { fleetName: name })
    const untrusted = hiddenReason({ ...amy, trust_level: 0 }, hit({}), { fleetName: name })
    const rows = hiddenFrom(amy, [hit({ id: "a" }), hit({ id: "b", scope: "self" })], [hit({ id: "a" })], { fleetName: name })
    const chain = [{ retired_agent: "old", successor: "heir" }, { retired_agent: "older", successor: "old" }]
    const owned = ownedAuthors("heir", chain)
    const heir = { name: "heir", trust_level: 2, home_fleet_id: "f1" }
    const inherited = [
      hiddenReason(heir, hit({ scope: "self", author: "older" }), { fleetName: name, owned }),
      hiddenReason(heir, hit({ scope: "self", author: "older" }), { fleetName: name }),
      hiddenReason(heir, hit({ fleet_id: "f2", author: "old" }), { fleetName: name, owned }),
    ]
    const inactive = hiddenReason(amy, hit({ state: "withdrawn" }), { fleetName: name, includeInactive: true })
    console.log(JSON.stringify({
      owned: [...owned].sort(), inherited, inactive, banner: asAgentBanner(amy, name), reasons, privileged, untrusted,
      rows: rows.map((r) => [r.hit.id, r.reason]),
    }))
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, (STATIC_DIR / "asagent.js").as_uri()],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    got = json.loads(out.stdout)
    assert got["banner"] == "Results as amy sees them (contributor, home fleet blue)"
    assert got["reasons"] == [
        "a private note of bob",
        "in fleet red, not its home fleet",
        "ranked below the cut-off or filtered",
        "withdrawn, so out of an ordinary search",
        "ranked below the cut-off or filtered",
    ]
    assert got["owned"] == ["heir", "old", "older"]
    assert got["inherited"] == [
        "ranked below the cut-off or filtered",
        "a private note of older",
        "ranked below the cut-off or filtered",
    ]
    assert got["inactive"] == "ranked below the cut-off or filtered"
    assert got["privileged"] == "ranked below the cut-off or filtered"
    assert got["untrusted"] == "untrusted agents read no memories"
    assert got["rows"] == [["b", "a private note of bob"]]


EXPLAIN = {
    "mode": "hybrid",
    "keyword_rank": 1,
    "vector_rank": 3,
    "fused": 0.03125,
    "recency": {"hint": "default", "age_days": 40.2, "half_life_days": 30.0, "decay": 0.3948,
                "floor": 0.8, "floor_applied": True, "factor": 0.8},
    "feedback": {"helpful": 2, "stale": 0, "wrong": 0, "flags_halved": False, "quality": 1.05},
    "social": {"cap": 1.2, "cap_applied": False, "factor": 1.05},
    "importance": {"value": 3, "factor": 1.0},
    "score": 0.02756,
}  # fmt: skip


def _explain_lines(*explains: dict) -> list[list[str]]:  # type: ignore[type-arg]
    script = r"""
    const { explainLines } = await import(process.argv[1])
    console.log(JSON.stringify(JSON.parse(process.argv[2]).map(explainLines)))
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, (STATIC_DIR / "explain.js").as_uri(),
         json.dumps(explains)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return json.loads(out.stdout)  # type: ignore[no-any-return]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_ranking_explanation_reads_in_plain_words() -> None:
    """ADR 0137: explain.js run as it ships: one plain line per part of the score."""
    (lines,) = _explain_lines(EXPLAIN)
    assert lines == [
        "Keyword: rank 1",
        "Vector: rank 3",
        "Fused (RRF): 0.03125",
        "Recency x0.8 (memory date 40 days ago; the 0.8 floor applied)",
        "Feedback x1.05 (2 helpful)",
        "Importance x1 (importance 3)",
        "Score 0.02756",
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_ranking_explanation_names_each_special_case() -> None:
    keyword_only = {**EXPLAIN, "mode": "keyword_only", "vector_rank": None}
    ignored = {
        **EXPLAIN,
        "recency": {
            **EXPLAIN["recency"],
            "hint": "ignore_age",
            "half_life_days": None,
            "floor": None,
            "floor_applied": False,
            "factor": 1.0,
        },
    }
    capped = {
        **EXPLAIN,
        "keyword_rank": None,
        "social": {"cap": 1.2, "cap_applied": True, "factor": 1.2},
        "feedback": {"helpful": 9, "stale": 1, "wrong": 0, "flags_halved": True, "quality": 1.4},
    }
    preferred = {
        **EXPLAIN,
        "recency": {
            **EXPLAIN["recency"],
            "hint": "prefer_recent",
            "floor_applied": False,
            "factor": 0.3948,
        },
    }
    plain = {
        **EXPLAIN,
        "feedback": {**EXPLAIN["feedback"], "helpful": 0, "quality": 1.0},
        "social": {"cap": 1.2, "cap_applied": False, "factor": 1.0},
    }
    got = _explain_lines(keyword_only, ignored, capped, preferred, plain)  # fmt: skip
    assert got[0][1] == "Vector: unavailable, this answer is keyword-only"
    assert got[1][3] == "Recency x1 (this search ignores age)"
    assert got[2][0] == "Keyword: not in the keyword results"
    assert "Feedback x1.4 (9 helpful, 1 stale, flags at half weight)" in got[2]
    assert "Social cap x1.2 (the feedback factor was held at the cap)" in got[2]
    assert got[3][3] == "Recency x0.3948 (memory date 40 days ago; hint prefer_recent)"
    assert got[4][4] == "Feedback x1 (no feedback)"
    assert not any(line.startswith("Social cap") for line in got[0] + got[3] + got[4])


def test_the_memory_browser_asks_for_the_explanation_and_draws_it_as_text() -> None:
    script = (STATIC_DIR / "memories.js").read_text()
    assert "body.explain = true" in script
    assert "Explain ranking" in script
    assert "innerHTML" not in script


def test_csv_exports_leave_the_explanation_out() -> None:
    """The CSV has no column for it, so an export never asks the search for it."""
    lists = (STATIC_DIR / "lists.js").read_text()
    assert "fetchPage({ ...page, exporting: true })" in lists
    portal_views = (STATIC_DIR.parent.parent / "portal" / "static" / "views.js").read_text()
    assert "if (f.explain && !exporting) body.explain = true;" in portal_views
    assert (
        'fetchItems({ ...(applied || filters()), explain: "" }'
        in (STATIC_DIR / "memories.js").read_text()
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_memory_filters_become_api_fields_with_whole_day_ranges() -> None:
    """memfilters.js run as it ships: the form values, the old hash keys and the day bounds."""
    script = r"""
    const m = await import(process.argv[1])
    const hash = (o) => (k) => o[k] || ""
    const none = { state: "active", tags: "", entity: "", created_from: "", created_to: "", occurred_from: "", occurred_to: "" }
    console.log(JSON.stringify({
      plain: m.filterFields(none),
      all: m.filterFields({ ...none, state: "all" }),
      full: m.filterFields({
        state: "withdrawn", tags: " a, b ,,", entity: "Redis", created_from: "2026-11-01", created_to: "2026-11-01",
        occurred_from: "2026-10-01", occurred_to: "2026-10-31",
      }),
      names: [m.parseNames("x,  y ,"), m.addName("a, b", "B"), m.addName("a", "c")],
      states: [
        m.stateFromHash(hash({ state: "superseded" }), "active"), m.stateFromHash(hash({ all: "1" }), "active"),
        m.stateFromHash(hash({ active: "1" }), "all"), m.stateFromHash(hash({}), "all"), m.stateFromHash(hash({ state: "bogus" }), "active"),
      ],
    }))
    """
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, (STATIC_DIR / "memfilters.js").as_uri()],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    got = json.loads(out.stdout)
    assert got["plain"] == {"state": "active"}
    assert got["all"] == {"include_inactive": True}
    assert got["full"] == {
        "state": "withdrawn",
        "tags": ["a", "b"],
        "entities": ["Redis"],
        "created_from": "2026-11-01T00:00:00Z",
        "created_to": "2026-11-01T23:59:59.999999Z",
        "occurred_from": "2026-10-01T00:00:00Z",
        "occurred_to": "2026-10-31T23:59:59.999999Z",
    }
    assert got["names"] == [["x", "y"], "a, b", "a, c"]
    assert got["states"] == ["superseded", "all", "active", "all", "active"]


def test_filter_chips_are_buttons_that_do_not_open_the_row() -> None:
    ui = (STATIC_DIR / "ui.js").read_text()
    assert "stopPropagation()" in ui and 'type: "button", class: "chip chip-button"' in ui
    lists = (STATIC_DIR / "lists.js").read_text()
    assert "onTag ? chipButton(" in lists and "event.target === row" in lists
    drawer = (STATIC_DIR / "drawer.js").read_text()
    assert "chipActions.onTag" in drawer and "chipActions.onEntity" in drawer
    memories = (STATIC_DIR / "memories.js").read_text()
    assert "memoryFilterFields(get" in memories and "filterFields(f)" in memories
    assert "Include inactive" not in memories
