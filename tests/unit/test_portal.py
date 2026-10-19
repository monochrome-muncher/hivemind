"""The agent portal runner (``hivemind-portal``, ADR 0095).

Pins the portal's allowlist (agent-key reads and its own routes, nothing
of the data plane's writes or the admin surface), which admin panel files
it serves, the security headers, the static bundle's rules, and a sign-in
end to end through the proxy into an in-process ``hivemind-api``.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

from hivemind.admin.app import STATIC_DIR as ADMIN_STATIC_DIR
from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore
from hivemind.portal.app import SHARED_MODULES, STATIC_DIR, create_portal_app, is_allowed
from hivemind.ports import Credential
from hivemind.webapp import CONTENT_SECURITY_POLICY
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config

AGENT = {"X-API-Key": "hm_agent_carol"}
ADMIN = {"X-API-Key": "hm_admin"}


def _api():
    hivemind = create_app_for_config(
        Settings(),
        store=MemoryStore(make_clock()),
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(
            agent_credentials={
                "hm_agent_carol": Credential(
                    user_id="carol",
                    agent_name="carol",
                    access_controlled=True,
                    trust_level=TrustLevel.CONTRIBUTOR,
                )
            }
        ),
        search_config=make_search_config(),
    )
    return hivemind, create_app(hivemind)


def _portal(transport: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    app = create_portal_app("http://hivemind-api.test", transport=transport)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://portal.test")


# -- the allowlist -----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "health"),
        ("GET", "whoami"),
        ("GET", "entries/abc123"),
        ("GET", "pins"),
        ("GET", "portal/entries"),
        ("POST", "portal/search"),
        ("GET", "portal/feedback"),
        ("POST", "portal/entries/abc123/withdraw"),
    ],
)
def test_the_portal_routes_are_forwarded(method: str, path: str) -> None:
    assert is_allowed(method, path)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        # The data plane's writes and its counted search.
        ("POST", "entries"),
        ("POST", "search"),
        ("GET", "entries"),
        ("POST", "entries/get"),
        ("POST", "entries/x/withdraw"),
        ("POST", "entries/x/feedback"),
        ("POST", "feedback"),
        ("PUT", "entries/x/pin"),
        ("DELETE", "entries/x/pin"),
        ("POST", "agents"),
        # The admin surface and the counters.
        ("GET", "metrics"),
        ("GET", "admin/entries"),
        ("GET", "admin/feedback"),
        ("POST", "admin/entries/x/withdraw"),
        ("GET", "admin/agents"),
        # Right path, wrong method, or one segment too many.
        ("GET", "portal/search"),
        ("POST", "portal/entries"),
        ("POST", "portal/feedback"),
        ("GET", "portal/entries/x/withdraw"),
        ("GET", "portal/entries/x"),
        ("GET", "entries/x/extra"),
        ("GET", "entries/"),
        ("GET", "pins/x"),
    ],
)
def test_everything_else_is_refused(method: str, path: str) -> None:
    assert not is_allowed(method, path)


async def test_a_refused_call_never_reaches_the_api() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    async with _portal(httpx.MockTransport(handler)) as portal:
        write = await portal.post("/v1/entries", json={"summary": "x"}, headers=AGENT)
        admin = await portal.get("/v1/admin/entries", headers=ADMIN)
    assert write.status_code == admin.status_code == 404
    assert write.json()["error"] == {
        "code": "not_proxied",
        "message": "the agent portal does not forward this call",
    }
    assert seen == []


# -- end to end through the proxy ---------------------------------------------------


async def test_an_agent_key_reads_and_withdraws_through_the_portal() -> None:
    hivemind, api_app = _api()
    summary = "kaniko needs a cache"
    entry = await hivemind.store.create_entry(
        EntryDraft(kind=Kind.FACT, summary=summary, author="carol", agent="a", scope="self"),
        await make_embedder().embed_text(summary),
    )
    entry_id = entry.id
    async with _portal(httpx.ASGITransport(app=api_app)) as portal:
        whoami = await portal.get("/v1/whoami", headers=AGENT)
        listed = await portal.get("/v1/portal/entries", params={"author": "carol"}, headers=AGENT)
        hits = await portal.post("/v1/portal/search", json={"query": "kaniko"}, headers=AGENT)
        detail = await portal.get(f"/v1/entries/{entry_id}?history=true", headers=AGENT)
        admin = await portal.get("/v1/portal/entries", headers=ADMIN)
        gone = await portal.post(
            f"/v1/portal/entries/{entry_id}/withdraw", json={"reason": "moved"}, headers=AGENT
        )
    assert whoami.json()["key_kind"] == "agent"
    assert [e["id"] for e in listed.json()] == [entry_id]
    assert [h["entry_id"] for h in hits.json()] == [entry_id]
    assert detail.json()["history"] == {"successors": [], "superseded": []}
    assert admin.status_code == 403  # the admin key belongs in the admin panel
    assert gone.json()["state"] == "withdrawn"
    assert await hivemind.store.search_counts() == []
    assert gone.headers["cache-control"] == "no-store"


# -- the static bundle --------------------------------------------------------------


async def test_the_portal_serves_its_page_and_only_the_shared_admin_files() -> None:
    _, api_app = _api()
    async with _portal(httpx.ASGITransport(app=api_app)) as portal:
        page = await portal.get("/")
        own = await portal.get("/static/portal.js")
        shared = {name: await portal.get(f"/static/{name}") for name in SHARED_MODULES}
        admin_only = [
            await portal.get(f"/static/{name}")
            for name in ("admin.js", "core.js", "manage.js", "index.html", "memories.js")
        ]
        health = await portal.get("/healthz")
    assert page.status_code == own.status_code == health.status_code == 200
    assert "agent portal" in page.text
    for name, resp in shared.items():
        assert resp.status_code == 200, name
        assert resp.content == (ADMIN_STATIC_DIR / name).read_bytes(), name
    # index.html at /static/ is the portal's own page, never the admin panel's.
    assert [r.status_code for r in admin_only[:3]] == [404, 404, 404]
    assert admin_only[3].content == (STATIC_DIR / "index.html").read_bytes()
    assert admin_only[4].status_code == 404
    for resp in (page, own, *shared.values(), health):
        assert resp.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert resp.headers["x-frame-options"] == "DENY"


def test_the_portal_and_admin_files_do_not_collide() -> None:
    """A portal file named like a shared one would be shadowed by it."""
    own = {p.name for p in STATIC_DIR.iterdir()}
    assert own.isdisjoint(SHARED_MODULES)
    for name in SHARED_MODULES:
        assert (ADMIN_STATIC_DIR / name).is_file(), name


def _scripts() -> list[Path]:
    """The portal's scripts plus the shared ones it serves."""
    own = sorted(STATIC_DIR.glob("*.js"))
    assert own, "no portal script found"
    return own + [ADMIN_STATIC_DIR / n for n in SHARED_MODULES if n.endswith(".js")]


def test_the_page_has_no_inline_script_or_style() -> None:
    html = (STATIC_DIR / "index.html").read_text()
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html, flags=re.IGNORECASE) == []
    assert re.search(r"\sstyle=", html) is None
    assert re.search(r"\son[a-z]+=", html) is None
    assert "<style" not in html
    assert re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", html) == ["/static/portal.js"]


def test_the_portal_scripts_never_write_html_or_build_links_from_data() -> None:
    for script in sorted(STATIC_DIR.glob("*.js")):
        js = script.read_text()
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert sink not in js, f"{script.name}: {sink}"
        assert re.findall(r"https?://[^\s\"'`]+", js) == [], script.name
        assert ".href =" not in js and 'setAttribute("href"' not in js, script.name
        for href in re.findall(r"\b(?:href|Href):\s*([^,}]+)", js):
            assert re.search(r"\bviewHref\(", href) or re.match(r"""\s*(?:["'`]#|null)""", href), (
                script.name,
                href,
            )
        assert "root.replaceChildren(" not in js, script.name


def test_the_scripts_load_only_modules_the_portal_serves() -> None:
    served = {p.name for p in _scripts()}
    for script in _scripts():
        for target in re.findall(r"""from\s+["']([^"']+)["']""", script.read_text()):
            assert target.startswith("./"), (script.name, target)
            assert target[2:] in served, (script.name, target)


def test_every_api_path_the_scripts_call_is_on_the_allowlist() -> None:
    call = re.compile(r"""\bapi(?:Raw)?\(\s*["']([A-Z]+)["']\s*,\s*([`"'])(.+?)\2""")
    seen: set[tuple[str, str]] = set()
    for script in _scripts():
        for method, _quote, path in call.findall(script.read_text()):
            seen.add((method, re.sub(r"\$\{[^}]*\}", "x", path)))
    assert ("POST", "portal/search") in seen  # the regex really finds calls
    assert len(seen) >= 7
    for method, path in sorted(seen):
        assert is_allowed(method, path), (method, path)


def test_the_portal_keeps_its_key_in_its_own_slot() -> None:
    session = (STATIC_DIR / "session.js").read_text()
    assert 'KEY_STORAGE = "hivemind.portalKey"' in session
    assert "hivemind.adminKey" not in "".join(p.read_text() for p in STATIC_DIR.glob("*.js"))


def test_the_runner_refuses_to_start_without_an_api_url(monkeypatch) -> None:
    from hivemind.portal import main

    monkeypatch.delenv("HIVEMIND_PORTAL_API_URL", raising=False)
    monkeypatch.setattr(main, "uvicorn", None)  # would crash if reached
    with pytest.raises(SystemExit, match="HIVEMIND_PORTAL_API_URL"):
        main.run()
