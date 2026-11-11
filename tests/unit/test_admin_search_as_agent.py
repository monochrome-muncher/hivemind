"""``POST /v1/admin/search`` with ``as_agent`` (ADR 0136): the admin sees what
one agent's own ``POST /v1/search`` returns, uncounted, for the admin key only."""

from __future__ import annotations

import httpx
import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config

ADMIN = {"X-API-Key": "hm_admin"}
QUERY = "kaniko cache"


class World:
    def __init__(self) -> None:
        self.store = MemoryStore(make_clock())
        self.auth = FakeAuthenticator(store=self.store)
        self.app = create_app_for_config(
            Settings(),
            store=self.store,
            embedder=make_embedder(),
            authenticator=self.auth,
            search_config=make_search_config(),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.app)), base_url="http://t"
        )
        self.fleets: dict[str, str] = {}
        self.keys: dict[str, str] = {}

    async def fleet(self, name: str) -> str:
        self.fleets[name] = (await self.store.create_fleet(name)).id
        return self.fleets[name]

    async def agent(self, name: str, fleet: str, level: TrustLevel) -> None:
        await self.store.register_agent(name)
        await self.store.activate_agent(name, trust_level=level, home_fleet_id=self.fleets[fleet])
        self.keys[name] = await self.auth.issue_agent_key(name)

    async def entry(self, author: str, summary: str, scope: str, fleet: str | None = None):  # type: ignore[no-untyped-def]
        draft = EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author=author,
            agent=author,
            scope=scope,
            fleet_id=self.fleets[fleet] if fleet else None,
        )
        return await self.store.create_entry(draft, await make_embedder().embed_text(summary))

    async def own_search(self, name: str) -> list[str]:
        resp = await self.client.post(
            "/v1/search", json={"query": QUERY}, headers={"X-API-Key": self.keys[name]}
        )
        assert resp.status_code == 200, resp.text
        return [h["entry_id"] for h in resp.json()]

    async def admin_search(self, **body: object) -> httpx.Response:
        return await self.client.post(
            "/v1/admin/search", json={"query": QUERY, **body}, headers=ADMIN
        )

    async def as_ids(self, name: str, **body: object) -> list[str]:
        resp = await self.admin_search(as_agent=name, **body)
        assert resp.status_code == 200, resp.text
        return [h["entry_id"] for h in resp.json()]


@pytest.fixture
async def w() -> World:
    world = World()
    for fleet in ("blue", "red"):
        await world.fleet(fleet)
    await world.agent("amy", "blue", TrustLevel.CONTRIBUTOR)
    await world.agent("pat", "blue", TrustLevel.PRIVILEGED)
    await world.agent("unt", "blue", TrustLevel.UNTRUSTED)
    await world.agent("old", "red", TrustLevel.CONTRIBUTOR)
    await world.agent("heir", "blue", TrustLevel.CONTRIBUTOR)
    await world.entry("amy", "amy private note kaniko cache", "self")
    await world.entry("bob", "bob private note kaniko cache", "self")
    await world.entry("old", "old private note kaniko cache", "self")
    await world.entry("bob", "blue fleet kaniko cache setup", "fleet", "blue")
    await world.entry("bob", "red fleet kaniko cache setup", "fleet", "red")
    await world.store.retire_agent("old", "heir", "admin")
    return world


@pytest.mark.parametrize("name", ["amy", "pat", "heir"])
async def test_the_results_are_the_agents_own_search_in_the_same_order(w: World, name: str) -> None:
    own = await w.own_search(name)
    assert own
    assert await w.as_ids(name) == own


async def test_agents_of_different_standing_see_different_things(w: World) -> None:
    seen = {name: set(await w.as_ids(name)) for name in ("amy", "pat", "heir")}
    assert len(set(map(frozenset, seen.values()))) == 3
    everything = await w.admin_search()
    assert len(everything.json()) == 5 > max(map(len, seen.values()))


async def test_a_successor_also_finds_what_its_retired_agent_wrote(w: World) -> None:
    summaries = {h["summary"] for h in (await w.admin_search(as_agent="heir")).json()}
    assert "old private note kaniko cache" in summaries


async def test_the_fleet_filter_still_applies_on_top(w: World) -> None:
    pat_only_blue = await w.admin_search(as_agent="pat", fleet_id=w.fleets["blue"])
    assert [h["summary"] for h in pat_only_blue.json()] == ["blue fleet kaniko cache setup"]


async def test_the_search_is_not_counted(w: World) -> None:
    await w.as_ids("amy")
    assert await w.store.search_counts() == []


async def test_an_unknown_agent_is_a_404_unknown_agent(w: World) -> None:
    resp = await w.admin_search(as_agent="ghost")
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "unknown_agent")


async def test_a_revoked_agent_is_a_409_agent_not_active(w: World) -> None:
    await w.store.revoke_agent("amy")
    resp = await w.admin_search(as_agent="amy")
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "agent_not_active")


async def test_a_pending_agent_is_a_409_agent_not_active(w: World) -> None:
    await w.store.register_agent("newbie")
    resp = await w.admin_search(as_agent="newbie")
    assert (resp.status_code, resp.json()["error"]["code"]) == (409, "agent_not_active")


async def test_an_agent_key_cannot_search_as_another_agent(w: World) -> None:
    resp = await w.client.post(
        "/v1/admin/search",
        json={"query": QUERY, "as_agent": "pat"},
        headers={"X-API-Key": w.keys["amy"]},
    )
    assert resp.status_code == 403


async def test_nothing_is_audited_by_an_as_agent_search(w: World) -> None:
    from hivemind.domain.audit import AuditFilters

    before = await w.store.list_audit(AuditFilters(), 100)
    await w.as_ids("amy")
    assert await w.store.list_audit(AuditFilters(), 100) == before


async def test_an_untrusted_agent_finds_nothing(w: World) -> None:
    await w.entry("unt", "unt private note kaniko cache", "self")
    assert await w.own_search("unt") == []
    assert await w.as_ids("unt") == []
