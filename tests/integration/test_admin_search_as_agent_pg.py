"""``POST /v1/admin/search`` with ``as_agent`` on real Postgres (ADR 0136): the
admin's answer equals each agent's own ``POST /v1/search``, uncounted, with
real keys, trust levels, fleets and a succession chain."""

from __future__ import annotations

import httpx
import pytest
from tests.fakes import FakeEmbedder, make_search_config
from tests.pg import ensure_migrated, reset_tables

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.keys import _issue_admin

QUERY = "kaniko cache"
AGENTS = (
    ("amy", "blue", TrustLevel.CONTRIBUTOR),
    ("pat", "blue", TrustLevel.PRIVILEGED),
    ("unt", "blue", TrustLevel.UNTRUSTED),
    ("old", "red", TrustLevel.CONTRIBUTOR),
    ("heir", "blue", TrustLevel.CONTRIBUTOR),
)


class World:
    def __init__(self, client: httpx.AsyncClient, store: PgStore, admin_key: str) -> None:
        self.client = client
        self.store = store
        self.fleets: dict[str, str] = {}
        self.admin = {"X-API-Key": admin_key}
        self.keys: dict[str, str] = {}

    async def post(self, path: str, headers: dict[str, str], **body: object) -> httpx.Response:
        return await self.client.post(path, json={"query": QUERY, **body}, headers=headers)

    async def own(self, name: str) -> list[str]:
        resp = await self.post("/v1/search", {"X-API-Key": self.keys[name]})
        assert resp.status_code == 200, resp.text
        return [h["entry_id"] for h in resp.json()]

    async def as_agent(self, name: str, **body: object) -> httpx.Response:
        return await self.post("/v1/admin/search", self.admin, as_agent=name, **body)


@pytest.fixture
async def world():
    settings = Settings()
    dsn = settings.database_url
    await ensure_migrated(dsn, settings.embedding_dim)
    await reset_tables(dsn)
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    app = create_app_for_config(
        settings,
        store=store,
        embedder=FakeEmbedder(dimension=settings.embedding_dim),
        authenticator=auth,
        search_config=make_search_config(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://t"
    )
    admin_key = await _issue_admin(dsn, actor="test")
    w = World(client, store, admin_key)
    admin = await auth.verify(admin_key)
    fleets = {n: (await app.access_service.create_fleet(n, admin)).id for n in ("blue", "red")}
    for name, fleet, level in AGENTS:
        await store.register_agent(name, "owner")
        _, w.keys[name] = await app.access_service.activate(name, level, fleets[fleet], admin)
    w.fleets = fleets
    try:
        yield w
    finally:
        await client.aclose()
        await store.close()
        await auth.close()


async def _seed(w: World) -> None:
    async def write(name: str, summary: str, **body: object) -> None:
        resp = await w.client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": summary, **body},
            headers={"X-API-Key": w.keys[name]},
        )
        assert resp.status_code == 201, resp.text

    await write("amy", "amy private note kaniko cache", scope="self")
    await write("old", "old private note kaniko cache", scope="self")
    await write("pat", "pat fleet kaniko cache setup", scope="fleet")
    await write("old", "red fleet kaniko cache setup", scope="fleet")
    retired = await w.client.post(
        "/v1/admin/agents/old/retire",
        json={"successor": "heir", "dry_run": False},
        headers=w.admin,
    )
    assert retired.status_code == 200, retired.text


@pytest.mark.parametrize("name", ["amy", "pat", "heir"])
async def test_the_admin_sees_what_the_agents_own_search_returns(world: World, name: str) -> None:
    await _seed(world)
    own = await world.own(name)
    assert own
    resp = await world.as_agent(name)
    assert [h["entry_id"] for h in resp.json()] == own


async def test_a_successor_finds_what_its_retired_agent_wrote(world: World) -> None:
    await _seed(world)
    summaries = {h["summary"] for h in (await world.as_agent("heir")).json()}
    assert "old private note kaniko cache" in summaries


async def test_the_fleet_filter_still_applies_on_top(world: World) -> None:
    await _seed(world)
    resp = await world.as_agent("pat", fleet_id=world.fleets["red"])
    assert [h["summary"] for h in resp.json()] == ["red fleet kaniko cache setup"]


async def test_the_search_is_not_counted_and_the_refusals_hold(world: World) -> None:
    await _seed(world)
    await world.as_agent("amy")
    assert await world.store.search_counts() == []
    unknown = await world.as_agent("ghost")
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "unknown_agent")
    retired = await world.as_agent("old")
    assert (retired.status_code, retired.json()["error"]["code"]) == (409, "agent_not_active")
