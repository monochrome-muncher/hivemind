"""Offboarding an owner on real Postgres (ADR 0135): real keys die with their agents, the audit
rows carry the owner alias, and a failure part-way rolls the whole offboarding back."""

from __future__ import annotations

import asyncpg
import httpx
import pytest
from tests.fakes import FakeEmbedder, make_search_config
from tests.pg import ensure_migrated, reset_tables

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, TrustLevel
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.offboarding import OffboardMode
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.keys import _issue_admin

ALIAS = "jane"


@pytest.fixture
async def world():
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    await reset_tables(dsn)
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=FakeEmbedder(dimension=Settings().embedding_dim),
        authenticator=auth,
        search_config=make_search_config(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://t"
    )
    admin_key = await _issue_admin(dsn, actor="test")
    admin = await auth.verify(admin_key)
    access = app.access_service
    fleet = (await access.create_fleet("red", admin)).id
    keys = {}
    for name, alias in (("jane-a", ALIAS), ("jane-b", ALIAS), ("heir", "kim")):
        await store.register_agent(name, alias)
        _, keys[name] = await access.activate(name, TrustLevel.CONTRIBUTOR, fleet, admin)
    await store.register_agent("jane-wait", ALIAS)
    try:
        yield dsn, store, client, {"X-API-Key": admin_key}, keys
    finally:
        await client.aclose()
        await store.close()
        await auth.close()


async def test_offboarding_kills_the_keys_and_audits_each_agent(world) -> None:
    _, store, client, admin, keys = world
    resp = await client.post(
        f"/v1/admin/owners/{ALIAS}/offboard",
        json={
            "mode": "retire",
            "successor": "heir",
            "dry_run": False,
            "expected_agents": [
                {"name": "jane-a", "action": "retire"},
                {"name": "jane-b", "action": "retire"},
                {"name": "jane-wait", "action": "reject"},
            ],
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert {a["name"]: a["action"] for a in resp.json()["agents"]} == {
        "jane-a": "retire",
        "jane-b": "retire",
        "jane-wait": "reject",
    }
    for name in ("jane-a", "jane-b"):
        assert (
            await client.get("/v1/whoami", headers={"X-API-Key": keys[name]})
        ).status_code == 401
    whoami = await client.get("/v1/whoami", headers={"X-API-Key": keys["heir"]})
    assert sorted(whoami.json()["inherits_from"]) == ["jane-a", "jane-b"]
    rows = await store.list_audit(AuditFilters(action=AuditAction.AGENT_RETIRE), 10)
    assert {r.target for r in rows} == {"jane-a", "jane-b"}
    assert all(r.detail["offboarded_owner"] == ALIAS for r in rows)


async def test_a_failure_for_one_agent_rolls_the_whole_offboarding_back(world) -> None:
    dsn, store, _, _, _ = world
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "CREATE FUNCTION fail_for_jane_b() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN IF NEW.retired_agent = 'jane-b' THEN RAISE EXCEPTION 'boom'; END IF; "
            "RETURN NEW; END $$"
        )
        await conn.execute(
            "CREATE TRIGGER fail_for_jane_b BEFORE INSERT ON successions "
            "FOR EACH ROW EXECUTE FUNCTION fail_for_jane_b()"
        )
        with pytest.raises(asyncpg.PostgresError, match="boom"):
            await store.offboard_owner(ALIAS, OffboardMode.RETIRE, "heir", "admin:1")
    finally:
        await conn.execute("DROP TRIGGER fail_for_jane_b ON successions")
        await conn.execute("DROP FUNCTION fail_for_jane_b()")
        await conn.close()
    assert await store.list_successions() == []
    statuses = {a.name: a.status for a in await store.list_agents()}
    assert statuses["jane-a"] is AgentStatus.ACTIVE and statuses["jane-b"] is AgentStatus.ACTIVE
    assert statuses["jane-wait"] is AgentStatus.PENDING
