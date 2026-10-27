"""What the admin panel and the agent portal share (``hivemind.webapp``).

Both runners are built on the same proxy app and ship a static bundle under
the same rules: a refused call never reaches the API, every response carries
the security headers and is revalidated, the page has no inline script, no
script writes HTML, and every API call a script makes is on the allowlist.
Each runner's own routes, files and links are pinned in ``test_admin_panel.py``
and ``test_portal.py``.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from hivemind.admin import app as admin_app
from hivemind.portal import app as portal_app
from hivemind.webapp import CONTENT_SECURITY_POLICY


@dataclass(frozen=True)
class Bundle:
    create: Callable[..., object]
    static_dir: Path
    is_allowed: Callable[[str, str], bool]
    main_module: str
    api_url_env: str
    script: str  # the one script the page loads
    scripts: list[Path]  # every script the bundle serves, shared ones included
    api_call: tuple[str, str]  # a call the regex must find, so it is not matching nothing
    min_api_calls: int
    refused: list[tuple[str, str]]  # calls the proxy must not forward
    name: str  # as the refusal message names the app
    extra_static: tuple[str, ...]  # other files served under /static


def _shared_scripts() -> list[Path]:
    return [admin_app.STATIC_DIR / n for n in portal_app.SHARED_MODULES if n.endswith(".js")]


BUNDLES = {
    "admin": Bundle(
        admin_app.create_admin_app,
        admin_app.STATIC_DIR,
        admin_app.is_allowed,
        "hivemind.admin.main",
        "HIVEMIND_ADMIN_API_URL",
        "admin.js",
        sorted(admin_app.STATIC_DIR.glob("*.js")),
        ("POST", "admin/search"),
        10,
        [("POST", "/v1/entries")],
        "the admin panel",
        ("admin.css",),
    ),
    "portal": Bundle(
        portal_app.create_portal_app,
        portal_app.STATIC_DIR,
        portal_app.is_allowed,
        "hivemind.portal.main",
        "HIVEMIND_PORTAL_API_URL",
        "portal.js",
        sorted(portal_app.STATIC_DIR.glob("*.js")) + _shared_scripts(),
        ("POST", "portal/search"),
        7,
        [("POST", "/v1/entries"), ("GET", "/v1/admin/entries")],
        "the agent portal",
        # Served from the admin bundle by exact-name routes (webapp.py).
        portal_app.SHARED_MODULES,
    ),
}

bundles = pytest.mark.parametrize("bundle", BUNDLES.values(), ids=BUNDLES.keys())


def _client(bundle: Bundle, transport: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    app = bundle.create("http://hivemind-api.test", transport=transport)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://web.test")  # type: ignore[arg-type]


@bundles
async def test_a_refused_call_never_reaches_the_api(bundle: Bundle) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    async with _client(bundle, httpx.MockTransport(handler)) as client:
        for method, path in bundle.refused:
            resp = await client.request(method, path, headers={"X-API-Key": "hm_key"})
            assert resp.status_code == 404, path
            assert resp.json()["error"]["code"] == "not_proxied", path
            assert f"{bundle.name} does not forward this call" in resp.text, path
    assert seen == []


@bundles
async def test_every_response_carries_the_security_headers_and_is_revalidated(
    bundle: Bundle,
) -> None:
    """A cached module would keep a tab on old code across a redeploy, so
    every full load revalidates against the server's ETag (``no-cache``)."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    async with _client(bundle, transport) as client:
        responses = [
            await client.get("/"),
            await client.get(f"/static/{bundle.script}"),
            *[await client.get(f"/static/{name}") for name in bundle.extra_static],
            await client.get("/healthz"),
            await client.get("/v1/health"),
        ]
    for resp in responses:
        assert resp.status_code == 200, resp.url
        assert resp.headers["content-security-policy"] == CONTENT_SECURITY_POLICY
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["referrer-policy"] == "no-referrer"
    for resp in responses[: 2 + len(bundle.extra_static)]:
        assert resp.headers["cache-control"] == "no-cache", resp.url


@bundles
def test_the_page_has_no_inline_script_or_style(bundle: Bundle) -> None:
    html = (bundle.static_dir / "index.html").read_text()
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html, flags=re.IGNORECASE) == []
    assert re.search(r"\sstyle=", html) is None
    assert re.search(r"\son[a-z]+=", html) is None
    assert "<style" not in html
    assert re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", html) == [f"/static/{bundle.script}"]


@bundles
def test_the_scripts_never_write_html(bundle: Bundle) -> None:
    sinks = (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "DOMParser",
        "createContextualFragment",
        "srcdoc",
        "new Function",
    )
    for script in sorted(bundle.static_dir.glob("*.js")):
        js = script.read_text()
        for sink in sinks:
            assert sink not in js, f"{script.name}: {sink}"
        assert re.search(r"\beval\(", js) is None, script.name
        # Loaded from nowhere but the bundle itself (the SVG namespace is an
        # identifier, not a fetch).
        urls = re.findall(r"https?://[^\s\"'`]+", js)
        assert set(urls) <= {"http://www.w3.org/2000/svg"}, script.name


@bundles
def test_every_api_path_the_scripts_call_is_on_the_allowlist(bundle: Bundle) -> None:
    """``api("GET", "admin/x")`` in the UI must be forwardable by the proxy."""
    call = re.compile(r"""\bapi(?:Raw)?\(\s*["']([A-Z]+)["']\s*,\s*([`"'])(.+?)\2""")
    seen: set[tuple[str, str]] = set()
    for script in bundle.scripts:
        for method, _quote, path in call.findall(script.read_text()):
            seen.add((method, re.sub(r"\$\{[^}]*\}", "x", path)))
    assert bundle.api_call in seen  # the regex really finds calls
    assert len(seen) >= bundle.min_api_calls
    for method, path in sorted(seen):
        assert bundle.is_allowed(method, path), (method, path)


@bundles
def test_the_runner_refuses_to_start_without_an_api_url(
    bundle: Bundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = importlib.import_module(bundle.main_module)
    monkeypatch.delenv(bundle.api_url_env, raising=False)
    monkeypatch.setattr(main, "uvicorn", None)  # would crash if reached
    with pytest.raises(SystemExit, match=bundle.api_url_env):
        main.run()
