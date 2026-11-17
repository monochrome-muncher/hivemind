"""An admin renames an agent, on Postgres with real keys (ADR 0159).

The agent's key keeps authenticating under the new name, what it wrote and rated follows, the old
name stays reserved, the history keeps the rows written before the rename, and the
``hivemind-keys rename-agent`` command does the same in one transaction.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

import asyncpg
import httpx
import pytest
from tests.fakes import FakeEmbedder, make_search_config
from tests.pg import ensure_migrated, exec_sql, fetch_rows, reset_tables

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store import keys as keys_cli
from hivemind.store.keys import _issue_admin, _rotate_org
from hivemind.store.pgstore import agent_rename


class World:
    def __init__(self, client: httpx.AsyncClient, dsn: str, admin_key: str) -> None:
        self.client = client
        self.dsn = dsn
        self.admin = {"X-API-Key": admin_key}
        self.keys: dict[str, str] = {}
        self.fleet = ""

    def as_agent(self, name: str) -> dict[str, str]:
        return {"X-API-Key": self.keys[name]}

    async def rename(self, old: str, new: str) -> httpx.Response:
        return await self.client.post(
            f"/v1/admin/agents/{old}/rename", json={"name": new}, headers=self.admin
        )

    async def write(self, name: str, summary: str, **body: object) -> str:
        resp = await self.client.post(
            "/v1/entries",
            json={"kind": "fact", "summary": summary, **body},
            headers=self.as_agent(name),
        )
        assert resp.status_code == 201, resp.text
        return str(resp.json()["id"])


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
    w = World(client, dsn, admin_key)
    admin = await auth.verify(admin_key)
    w.fleet = (await app.access_service.create_fleet("blue", admin)).id
    for name in ("bob", "amy"):
        await store.register_agent(name, "owner")
        _, w.keys[name] = await app.access_service.activate(
            name, TrustLevel.CONTRIBUTOR, w.fleet, admin
        )
    try:
        yield w
    finally:
        await client.aclose()
        await store.close()
        await auth.close()


async def test_the_key_keeps_working_and_whoami_shows_the_new_name(world: World) -> None:
    resp = await world.rename("bob", "robert")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "robert"

    whoami = await world.client.get("/v1/whoami", headers=world.as_agent("bob"))
    assert whoami.status_code == 200
    assert (whoami.json()["name"], whoami.json()["status"]) == ("robert", "active")
    assert (await world.client.get("/v1/whoami", headers=world.as_agent("amy"))).json()[
        "name"
    ] == "amy"


async def test_the_agent_still_owns_what_it_wrote_and_still_cannot_rate_it(world: World) -> None:
    note = await world.write("bob", "bob's private note about kaniko", scope="self")
    shared = await world.write("bob", "bob's fleet note about kaniko", scope="fleet")
    await world.rename("bob", "robert")

    read = await world.client.get(f"/v1/entries/{note}", headers=world.as_agent("bob"))
    assert read.status_code == 200 and read.json()["author"] == "robert"
    found = await world.client.post(
        "/v1/search", json={"query": "private note kaniko"}, headers=world.as_agent("bob")
    )
    assert note in [hit["entry_id"] for hit in found.json()]
    amy_search = await world.client.post(
        "/v1/search", json={"query": "private note kaniko"}, headers=world.as_agent("amy")
    )
    assert note not in [hit["entry_id"] for hit in amy_search.json()]

    rated = await world.client.post(
        f"/v1/entries/{shared}/feedback", json={"verdict": "helpful"}, headers=world.as_agent("bob")
    )
    assert rated.status_code == 403


async def test_every_stored_copy_of_the_name_follows(world: World) -> None:
    amy_note = await world.write("amy", "amy's note about kaniko")
    pinned = await world.write("bob", "bob's pinned fleet note", scope="fleet")
    rated = await world.client.post(
        f"/v1/entries/{amy_note}/feedback",
        json={"verdict": "helpful"},
        headers=world.as_agent("bob"),
    )
    assert rated.status_code == 200, rated.text
    pin = await world.client.put(f"/v1/entries/{pinned}/pin", headers=world.as_agent("bob"))
    assert pin.status_code == 200, pin.text

    await world.rename("bob", "robert")

    def names(rows: list[Any], *columns: str) -> set[tuple[Any, ...]]:
        return {tuple(r[c] for c in columns) for r in rows}

    assert names(
        await fetch_rows(world.dsn, "SELECT name FROM agents WHERE name IN ('bob', 'robert')"),
        "name",
    ) == {("robert",)}
    assert names(
        await fetch_rows(
            world.dsn, "SELECT user_id, agent_id, agent_name FROM credentials WHERE kind = 'agent'"
        ),
        "user_id",
        "agent_id",
        "agent_name",
    ) == {("robert",) * 3, ("amy",) * 3}
    assert names(
        await fetch_rows(world.dsn, "SELECT author, agent FROM entries WHERE author <> 'amy'"),
        "author",
        "agent",
    ) == {("robert", "robert")}
    assert names(
        await fetch_rows(world.dsn, 'SELECT "user", agent FROM feedbacks'), "user", "agent"
    ) == {("robert", "robert")}
    assert names(await fetch_rows(world.dsn, "SELECT pinned_by FROM pins"), "pinned_by") == {
        ("robert",)
    }
    assert names(
        await fetch_rows(world.dsn, "SELECT name, agent FROM agent_former_names"), "name", "agent"
    ) == {("bob", "robert")}


async def test_a_self_reported_agent_label_stays_as_written(world: World) -> None:
    resp = await world.client.post(
        "/v1/entries",
        json={"kind": "fact", "summary": "labelled", "agent": "claude-code"},
        headers=world.as_agent("bob"),
    )
    assert resp.status_code == 201
    await world.rename("bob", "robert")
    rows = await fetch_rows(world.dsn, "SELECT author, agent FROM entries")
    assert [(r["author"], r["agent"]) for r in rows] == [("robert", "robert")]


async def test_the_old_name_is_reserved_for_every_way_of_registering(world: World) -> None:
    await world.rename("bob", "robert")
    org_key = await _rotate_org(world.dsn, actor="test")
    for name in ("bob", "BOB"):
        registered = await world.client.post(
            "/v1/agents", json={"name": name, "owner_alias": "x"}, headers={"X-API-Key": org_key}
        )
        assert registered.status_code == 409
        assert registered.json()["error"]["code"] == "name_conflict"
        created = await world.client.post(
            "/v1/admin/agents",
            json={"name": name, "owner_alias": "x", "home_fleet_id": world.fleet},
            headers=world.admin,
        )
        assert created.status_code == 409
        assert created.json()["error"]["code"] == "name_conflict"
    assert (await world.rename("amy", "Bob")).status_code == 409
    assert (await world.rename("robert", "bob")).status_code == 200  # its own earlier name
    assert await fetch_rows(world.dsn, "SELECT * FROM agent_former_names WHERE name = 'bob'") == []


async def test_the_history_keeps_the_rows_written_before_the_rename(world: World) -> None:
    await world.rename("bob", "robert")

    log = await world.client.get(
        "/v1/admin/audit-log", params={"subject": "robert"}, headers=world.admin
    )
    pairs = {(r["action"], r["target"]) for r in log.json()}
    assert {("agent.activate", "bob"), ("agent.rename", "robert")} <= pairs
    assert not any(target == "amy" for _, target in pairs)

    portal = await world.client.get("/v1/portal/history", headers=world.as_agent("bob"))
    assert portal.status_code == 200, portal.text
    summaries = [item["summary"] for item in portal.json()]
    assert "Renamed from bob to robert" in summaries
    assert any(s.startswith("Activated as") for s in summaries)


async def test_the_rename_is_audited_with_the_admin_key_and_the_same_name_is_a_no_op(
    world: World,
) -> None:
    assert (await world.rename("bob", "bob")).status_code == 200
    await world.rename("bob", "robert")
    rows = await fetch_rows(
        world.dsn,
        "SELECT actor_kind, action, target, detail FROM audit_log WHERE action = 'agent.rename'",
    )
    assert len(rows) == 1
    assert (rows[0]["actor_kind"], rows[0]["target"]) == ("admin_key", "robert")
    assert json.loads(rows[0]["detail"]) == {"from": "bob", "to": "robert"}


# -- hivemind-keys rename-agent ------------------------------------------------


async def _keys(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    """Run ``hivemind-keys`` as the console script does (``main`` runs its own event loop)."""
    monkeypatch.setattr(sys, "argv", ["hivemind-keys", "--actor", "op", *argv])
    await asyncio.to_thread(keys_cli.main)


async def _renames(world: World) -> list[Any]:
    return await fetch_rows(
        world.dsn,
        "SELECT actor_kind, actor, target, detail FROM audit_log WHERE action = 'agent.rename'",
    )


async def test_the_cli_renames_in_one_transaction_and_audits_a_cli_row(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    await _keys(monkeypatch, "rename-agent", "bob", "robert")
    assert "renamed agent bob to robert" in capsys.readouterr().out
    assert (await world.client.get("/v1/whoami", headers=world.as_agent("bob"))).json()[
        "name"
    ] == "robert"
    rows = await _renames(world)
    assert [(r["actor_kind"], r["actor"], r["target"], json.loads(r["detail"])) for r in rows] == [
        ("cli", "op", "robert", {"from": "bob", "to": "robert"})
    ]
    await _keys(monkeypatch, "rename-agent", "robert", "robert")
    assert "nothing to do" in capsys.readouterr().out
    assert len(await _renames(world)) == 1


@pytest.mark.parametrize(
    ("old", "new"),
    [("ghost", "casper"), ("amy", "ROBERT"), ("amy", "bob"), ("amy", "admin")],
    ids=["unknown", "taken", "former", "reserved"],
)
async def test_the_cli_refuses_what_the_route_refuses(
    world: World, monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    await _keys(monkeypatch, "rename-agent", "bob", "robert")
    with pytest.raises(SystemExit) as exit_info:
        await _keys(monkeypatch, "rename-agent", old, new)
    assert exit_info.value.code == 1
    assert len(await _renames(world)) == 1


# -- legacy case variants and the write that races the rename ---------------------


async def test_two_legacy_case_variants_can_both_be_renamed(world: World) -> None:
    """ADR 0045 only stops new variants: `Dan` and `dan` may already coexist, and leaving both names
    reserves them once."""
    for name in ("Dan", "dan"):
        await exec_sql(
            world.dsn,
            "INSERT INTO agents (name, status, trust_level) VALUES ($1, 'active', 1)",
            name,
        )
    assert (await world.rename("Dan", "dana")).status_code == 200
    assert (await world.rename("dan", "danny")).status_code == 200
    rows = await fetch_rows(world.dsn, "SELECT name, agent FROM agent_former_names")
    assert [(r["name"], r["agent"]) for r in rows] == [("Dan", "dana")]
    assert (await world.rename("amy", "DAN")).status_code == 409


async def test_a_write_holding_the_agent_row_makes_the_rename_wait_and_is_rewritten(
    world: World,
) -> None:
    writer = await asyncpg.connect(world.dsn)
    try:
        tx = writer.transaction()
        await tx.start()
        await writer.fetchval("SELECT 1 FROM agents WHERE name = 'bob' FOR KEY SHARE")
        await writer.execute(
            "INSERT INTO entries (kind, summary, author, agent, scope, occurred_at) "
            "VALUES ('fact', 'in flight', 'bob', 'bob', 'org', now())"
        )
        renaming = asyncio.create_task(world.rename("bob", "robert"))
        await asyncio.sleep(0.5)
        assert not renaming.done()  # waiting for the write that holds the row
        await tx.commit()
    finally:
        await writer.close()
    assert (await renaming).status_code == 200
    rows = await fetch_rows(world.dsn, "SELECT author, agent FROM entries")
    assert [(r["author"], r["agent"]) for r in rows] == [("robert", "robert")]


async def test_a_rename_that_waits_too_long_for_a_lock_is_the_retryable_fleet_busy(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_rename, "MERGE_SETTINGS", "SET LOCAL lock_timeout = '200ms'")
    holder = await asyncpg.connect(world.dsn)
    try:
        tx = holder.transaction()
        await tx.start()
        await holder.fetchval("SELECT 1 FROM agents WHERE name = 'bob' FOR KEY SHARE")
        resp = await world.rename("bob", "robert")
        await tx.rollback()
    finally:
        await holder.close()
    assert (resp.status_code, resp.json()["error"]["code"]) == (503, "fleet_busy")
    assert (await world.rename("bob", "robert")).status_code == 200


async def test_a_successor_keeps_what_was_handed_down_after_both_are_renamed(
    world: World,
) -> None:
    note = await world.write("bob", "bob's private note about kaniko", scope="self")
    retired = await world.client.post(
        "/v1/admin/agents/bob/retire",
        json={"successor": "amy", "dry_run": False},
        headers=world.admin,
    )
    assert retired.status_code == 200, retired.text

    assert (await world.rename("bob", "robert")).status_code == 200
    assert (await world.rename("amy", "amelia")).status_code == 200

    whoami = await world.client.get("/v1/whoami", headers=world.as_agent("amy"))
    assert (whoami.json()["name"], whoami.json()["inherits_from"]) == ("amelia", ["robert"])
    read = await world.client.get(f"/v1/entries/{note}", headers=world.as_agent("amy"))
    assert read.status_code == 200 and read.json()["author"] == "robert"
    history = await world.client.get("/v1/portal/history", headers=world.as_agent("amy"))
    summaries = [item["summary"] for item in history.json()]
    assert "Renamed from bob to robert" in summaries
    assert "Renamed from amy to amelia" in summaries
