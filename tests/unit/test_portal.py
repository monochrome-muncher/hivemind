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
from hivemind.domain.audit import AuditFilters
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore
from hivemind.portal.app import SHARED_MODULES, STATIC_DIR, create_portal_app, is_allowed
from hivemind.ports import Credential
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
        ("GET", "portal/flagged"),
        ("POST", "portal/entries/abc123/withdraw"),
        ("PUT", "portal/entries/abc123/pin"),
        ("DELETE", "portal/entries/abc123/pin"),
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
        ("POST", "portal/flagged"),
        ("GET", "portal/flagged/x"),
        ("GET", "admin/flagged"),
        ("GET", "portal/entries/x/withdraw"),
        ("GET", "portal/entries/x"),
        ("GET", "entries/x/extra"),
        ("GET", "entries/"),
        ("GET", "pins/x"),
        ("POST", "pins"),
        ("PUT", "pins/x"),
        ("POST", "portal/entries/x/pin"),
        ("GET", "portal/entries/x/pin"),
        ("PUT", "portal/entries/x/pin/y"),
    ],
)
def test_everything_else_is_refused(method: str, path: str) -> None:
    assert not is_allowed(method, path)


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


# -- pin and unpin (ADR 0116) ------------------------------------------------------


def _fleet_api():
    def agent(name: str, level: TrustLevel, fleet: str = "ops") -> Credential:
        return Credential(
            user_id=name,
            agent_id=name,
            agent_name=name,
            access_controlled=True,
            trust_level=level,
            home_fleet_id=fleet,
        )

    store = MemoryStore(make_clock())
    hivemind = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(
            agent_credentials={
                "hm_agent_dev": agent("dev", TrustLevel.CONTRIBUTOR),
                "hm_agent_lead": agent("lead", TrustLevel.PRIVILEGED),
                "hm_agent_lurk": agent("lurk", TrustLevel.LURKER),
                "hm_agent_away": agent("away", TrustLevel.CONTRIBUTOR, "other"),
                "hm_agent_heir": Credential(
                    user_id="heir",
                    agent_id="heir",
                    agent_name="heir",
                    access_controlled=True,
                    trust_level=TrustLevel.CONTRIBUTOR,
                    home_fleet_id="ops",
                    inherits_from=("old",),
                ),
            }
        ),
        search_config=make_search_config(),
    )
    return store, create_app(hivemind)


async def _fleet_entry(store: MemoryStore, summary: str, scope: str = "fleet") -> str:
    entry = await store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author="dev",
            agent="dev",
            scope=scope,
            fleet_id="ops" if scope == "fleet" else None,
        ),
        await make_embedder().embed_text(summary),
    )
    return entry.id


def _key(name: str) -> dict[str, str]:
    return {"X-API-Key": f"hm_agent_{name}"}


async def test_an_agent_pins_and_unpins_through_the_portal_and_it_is_audited() -> None:
    store, api_app = _fleet_api()
    entry_id = await _fleet_entry(store, "deploys go through the runner pool")
    async with _portal(httpx.ASGITransport(app=api_app)) as portal:
        pinned = await portal.put(f"/v1/portal/entries/{entry_id}/pin", headers=_key("dev"))
        shown = await portal.get("/v1/pins", headers=_key("lead"))
        # A privileged agent may unpin someone else's pin.
        unpinned = await portal.delete(f"/v1/portal/entries/{entry_id}/pin", headers=_key("lead"))
        again = await portal.delete(f"/v1/portal/entries/{entry_id}/pin", headers=_key("lead"))
    assert pinned.status_code == 200
    assert [p["id"] for p in shown.json()["pins"]] == [entry_id]
    assert unpinned.json() == {"entry_id": entry_id, "removed": True}
    assert again.json()["removed"] is False
    rows = list(reversed(await store.list_audit(AuditFilters(), 50)))
    assert [(r.action.value, r.actor_kind.value, r.actor) for r in rows] == [
        ("entry.pin", "agent_key", "dev"),
        ("entry.unpin", "agent_key", "lead"),
    ]
    assert rows[0].detail["via"] == rows[1].detail["via"] == "portal"


async def test_portal_pins_keep_the_pin_rules_and_the_agent_key_gate() -> None:
    store, api_app = _fleet_api()
    entry_id = await _fleet_entry(store, "a fleet rule")
    private_id = await _fleet_entry(store, "a private note", scope="self")
    async with _portal(httpx.ASGITransport(app=api_app)) as portal:
        url = f"/v1/portal/entries/{entry_id}/pin"
        lurker = await portal.put(url, headers=_key("lurk"))
        away = await portal.put(url, headers=_key("away"))
        admin = await portal.put(url, headers=ADMIN)
        org = await portal.put(url, headers={"X-API-Key": "hm_org"})
        admin_unpin = await portal.delete(url, headers=ADMIN)
        not_fleet = await portal.put(f"/v1/portal/entries/{private_id}/pin", headers=_key("dev"))
        unknown = await portal.put("/v1/portal/entries/nope/pin", headers=_key("dev"))
        direct = await portal.put(f"/v1/entries/{entry_id}/pin", headers=_key("dev"))
    assert lurker.status_code == 403
    assert away.status_code == 404  # another fleet's entry is not visible to it
    assert admin.status_code == admin_unpin.status_code == org.status_code == 403
    assert not_fleet.status_code == 422  # only fleet entries can be pinned
    assert unknown.status_code == 404
    assert direct.status_code == 404  # the data plane's pin stays off the portal proxy
    assert await store.list_audit(AuditFilters(), 50) == []


async def test_the_portal_pin_cap_is_three_per_agent_with_a_plain_message() -> None:
    store, api_app = _fleet_api()
    ids = [await _fleet_entry(store, f"rule number {n}") for n in range(4)]
    async with _portal(httpx.ASGITransport(app=api_app)) as portal:
        for entry_id in ids[:3]:
            ok = await portal.put(f"/v1/portal/entries/{entry_id}/pin", headers=_key("dev"))
            assert ok.status_code == 200
        over = await portal.put(f"/v1/portal/entries/{ids[3]}/pin", headers=_key("dev"))
    assert over.status_code == 422
    assert "unpin one first" in over.json()["error"]["message"]


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


def test_the_portal_scripts_never_build_links_from_data() -> None:
    for script in sorted(STATIC_DIR.glob("*.js")):
        js = script.read_text()
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


def test_the_portal_keeps_its_key_in_its_own_slot() -> None:
    session = (STATIC_DIR / "session.js").read_text()
    assert 'KEY_STORAGE = "hivemind.portalKey"' in session
    assert "hivemind.adminKey" not in "".join(p.read_text() for p in STATIC_DIR.glob("*.js"))


async def test_a_lurker_cannot_unpin_through_the_portal() -> None:
    store, api_app = _fleet_api()
    entry_id = await _fleet_entry(store, "a pinned rule")
    async with _portal(httpx.ASGITransport(app=api_app)) as portal:
        await portal.put(f"/v1/portal/entries/{entry_id}/pin", headers=_key("dev"))
        refused = await portal.delete(f"/v1/portal/entries/{entry_id}/pin", headers=_key("lurk"))
        still = await portal.get("/v1/pins", headers=_key("dev"))
    assert refused.status_code == 403
    assert [p["id"] for p in still.json()["pins"]] == [entry_id]


async def test_the_portal_fleet_cap_is_ten() -> None:
    store, api_app = _fleet_api()
    ids = [await _fleet_entry(store, f"fleet rule {n}") for n in range(11)]
    for n, entry_id in enumerate(ids[:10]):
        await store.pin_entry("ops", entry_id, f"member-{n}", 10, [], None, ())
    async with _portal(httpx.ASGITransport(app=api_app)) as portal:
        over = await portal.put(f"/v1/portal/entries/{ids[10]}/pin", headers=_key("dev"))
    assert over.status_code == 422
    assert "10 pinned entries" in over.json()["error"]["message"]


async def test_inherited_pins_count_toward_the_portal_cap_of_three() -> None:
    store, api_app = _fleet_api()
    ids = [await _fleet_entry(store, f"rule {n}") for n in range(4)]
    for entry_id in ids[:2]:  # pinned by an agent retired in the heir's favour
        await store.pin_entry("ops", entry_id, "old", 10, [], 3, ())
    async with _portal(httpx.ASGITransport(app=api_app)) as portal:
        third = await portal.put(f"/v1/portal/entries/{ids[2]}/pin", headers=_key("heir"))
        fourth = await portal.put(f"/v1/portal/entries/{ids[3]}/pin", headers=_key("heir"))
    assert third.status_code == 200
    assert fourth.status_code == 422
    assert "unpin one first" in fourth.json()["error"]["message"]


async def test_a_data_plane_pin_is_audited_without_via() -> None:
    store, api_app = _fleet_api()
    entry_id = await _fleet_entry(store, "a rule")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://api.test"
    ) as api:
        assert (
            await api.put(f"/v1/entries/{entry_id}/pin", headers=_key("dev"))
        ).status_code == 200
    (row,) = await store.list_audit(AuditFilters(), 50)
    assert "via" not in row.detail
