"""Retiring an agent with a successor, end to end on real Postgres (ADR 0113).

Real keys issued and verified by ``PgAuthenticator`` (whose one-query key
check resolves the chain with a recursive CTE), real ``PgStore``, the REST
app on top. Each ownership check is run both ways: the successor may, an
unrelated agent may not, the retired agent's key is dead, and an undo takes
it all back. Skips when Postgres is down, like the rest of the suite."""

from __future__ import annotations

import asyncio

import asyncpg
import httpx
import pytest
from tests.fakes import FakeEmbedder, make_search_config
from tests.pg import ensure_migrated

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, TrustLevel
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.succession import AgentRetired, SuccessionRefused
from hivemind.services.access import AccessService
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.keys import _issue_admin

TABLES = (
    "TRUNCATE audit_log, credentials, agents, fleets, entries, feedbacks, pins, "
    "entry_links, search_counts, successions"
)


@pytest.fixture
async def world():
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    conn = await asyncpg.connect(dsn)
    await conn.execute(TABLES)
    await conn.close()
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
    w = World(dsn, store, auth, app, client, admin_key)
    await w.setup()
    try:
        yield w
    finally:
        await client.aclose()
        await store.close()
        await auth.close()
        conn = await asyncpg.connect(dsn)
        await conn.execute(TABLES)
        await conn.close()


class World:
    def __init__(self, dsn, store, auth, app, client, admin_key) -> None:  # type: ignore[no-untyped-def]
        self.dsn, self.store, self.auth, self.app = dsn, store, auth, app
        self.client, self.admin_key = client, admin_key
        self.keys: dict[str, str] = {}
        self.fleets: dict[str, str] = {}

    @property
    def admin(self) -> dict[str, str]:
        return {"X-API-Key": self.admin_key}

    def h(self, name: str) -> dict[str, str]:
        return {"X-API-Key": self.keys[name]}

    async def setup(self) -> None:
        access = self.app.access_service
        admin = await self.auth.verify(self.admin_key)
        for fleet in ("red", "blue"):
            self.fleets[fleet] = (await access.create_fleet(fleet, admin)).id
        for name, fleet in (("old", "red"), ("heir", "blue"), ("other", "blue"), ("peer", "red")):
            await self.store.register_agent(name, "owner")
            _, key = await access.activate(name, TrustLevel.CONTRIBUTOR, self.fleets[fleet], admin)
            self.keys[name] = key

    async def write(self, name: str, summary: str, **body) -> dict:  # type: ignore[no-untyped-def]
        resp = await self.client.post(
            "/v1/entries", json={"kind": "fact", "summary": summary, **body}, headers=self.h(name)
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    async def status(self, name: str, entry_id: str) -> int:
        return (await self.client.get(f"/v1/entries/{entry_id}", headers=self.h(name))).status_code

    async def retire(self, retired: str = "old", successor: str = "heir") -> httpx.Response:
        return await self.client.post(
            f"/v1/admin/agents/{retired}/retire",
            json={"successor": successor, "dry_run": False},
            headers=self.admin,
        )


async def test_the_whole_flow_with_real_keys(world) -> None:
    w = world
    note = await w.write("old", "old private kaniko note", scope="self")
    red_fact = await w.write("old", "red kaniko fact", scope="fleet")
    assert red_fact["fleet_id"] == w.fleets["red"]
    pin = await w.client.put(f"/v1/entries/{red_fact['id']}/pin", headers=w.h("old"))
    assert pin.status_code == 200

    # Before: nobody else reads the note; heir (blue) cannot read the red fact.
    assert await w.status("heir", note["id"]) == 404
    assert await w.status("heir", red_fact["id"]) == 404

    preview = await w.client.post(
        "/v1/admin/agents/old/retire",
        json={"successor": "heir", "dry_run": True},
        headers=w.admin,
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["self_notes"] == 1 and preview.json()["fleet_memories"] == 1
    assert preview.json()["pins"] == 1 and preview.json()["fleet_memories_elsewhere"] == 1
    assert (await w.client.get("/v1/entries", headers=w.h("old"))).status_code == 200  # still live

    done = await w.retire()
    assert done.status_code == 200, done.text

    # The retired agent's key is dead (status revoked and the key row deleted).
    assert (await w.client.get("/v1/whoami", headers=w.h("old"))).status_code == 401
    conn = await asyncpg.connect(w.dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM credentials WHERE agent_name = 'old'") == 0
    finally:
        await conn.close()
    assert (await w.store.get_agent("old")).status is AgentStatus.REVOKED

    # The successor owns everything; an unrelated agent in its fleet owns nothing.
    whoami = await w.client.get("/v1/whoami", headers=w.h("heir"))
    assert whoami.json()["inherits_from"] == ["old"]
    assert (await w.client.get("/v1/whoami", headers=w.h("other"))).json()["inherits_from"] == []
    assert await w.status("heir", note["id"]) == 200
    assert await w.status("heir", red_fact["id"]) == 200
    assert await w.status("other", note["id"]) == 404
    assert await w.status("other", red_fact["id"]) == 404
    assert await w.status("peer", red_fact["id"]) == 200  # the red fleet reads it, as before
    assert (await w.status("peer", note["id"])) == 404

    # Search and the own-name list.
    hits = await w.client.post("/v1/search", json={"query": "kaniko"}, headers=w.h("heir"))
    assert {h["entry_id"] for h in hits.json()} == {note["id"], red_fact["id"]}
    nothing = await w.client.post("/v1/search", json={"query": "kaniko"}, headers=w.h("other"))
    assert nothing.json() == []
    own = await w.client.get("/v1/entries", params={"author": "heir"}, headers=w.h("heir"))
    assert {e["id"] for e in own.json()} == {note["id"], red_fact["id"]}
    portal = await w.client.get(
        "/v1/portal/entries", params={"author": "heir"}, headers=w.h("heir")
    )
    assert {e["id"] for e in portal.json()} == {note["id"], red_fact["id"]}

    # Rating: the successor cannot (it is its own), a reader that does not own it can.
    assert (
        await w.client.post(
            f"/v1/entries/{red_fact['id']}/feedback",
            json={"verdict": "helpful"},
            headers=w.h("heir"),
        )
    ).status_code == 403
    assert (
        await w.client.post(
            f"/v1/entries/{red_fact['id']}/feedback", json={"verdict": "stale"}, headers=w.h("peer")
        )
    ).status_code == 200
    received = await w.client.get(
        "/v1/portal/feedback", params={"direction": "received"}, headers=w.h("heir")
    )
    assert [i["entry"]["id"] for i in received.json()] == [red_fact["id"]]

    # Superseding: heir its note yes, an unrelated agent no; the red fleet fact, not into blue.
    denied = await w.client.post(
        "/v1/entries",
        json={"kind": "fact", "summary": "hijack", "scope": "self", "supersedes": [note["id"]]},
        headers=w.h("other"),
    )
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "supersede_denied"
    narrow = await w.client.post(
        "/v1/entries",
        json={"kind": "fact", "summary": "blue", "scope": "fleet", "supersedes": [red_fact["id"]]},
        headers=w.h("heir"),
    )
    assert narrow.status_code == 403
    newer = await w.write("heir", "old note, revised", scope="self", supersedes=[note["id"]])
    assert newer["author"] == "heir"
    assert (await w.store.get_entry(note["id"])).state.value == "superseded"

    # Withdrawing: an unrelated reader of the red fleet cannot, the successor can.
    assert (
        await w.client.post(f"/v1/entries/{red_fact['id']}/withdraw", json={}, headers=w.h("peer"))
    ).status_code == 403
    portal_withdraw = await w.client.post(
        f"/v1/portal/entries/{red_fact['id']}/withdraw",
        json={"reason": "obsolete"},
        headers=w.h("heir"),
    )
    assert portal_withdraw.status_code == 200, portal_withdraw.text
    assert await w.store.list_pins(w.fleets["red"]) == []  # a withdrawn entry leaves the briefing

    audited = {r.action for r in await w.store.list_audit(AuditFilters(), 100)}
    assert {AuditAction.AGENT_RETIRE, AuditAction.AGENT_REVOKE} <= audited


async def test_an_undo_takes_the_memories_back_and_the_old_key_stays_dead(world) -> None:
    w = world
    note = await w.write("old", "old private note", scope="self")
    assert (await w.retire()).status_code == 200
    assert await w.status("heir", note["id"]) == 200
    undone = await w.client.post("/v1/admin/successions/old/undo", headers=w.admin)
    assert undone.status_code == 200
    assert await w.status("heir", note["id"]) == 404
    assert (await w.client.get("/v1/whoami", headers=w.h("old"))).status_code == 401
    assert (await w.client.get("/v1/whoami", headers=w.h("heir"))).json()["inherits_from"] == []
    assert (await w.client.get("/v1/admin/successions", headers=w.admin)).json() == []
    assert await w.store.list_audit(AuditFilters(action=AuditAction.AGENT_RETIRE_UNDO), 10)


async def test_chains_resolve_in_the_key_check_and_cycles_are_refused(world) -> None:
    w = world
    a_note = await w.write("old", "a private note", scope="self")
    b_note = await w.write("heir", "b private note", scope="self")
    assert (await w.retire("old", "heir")).status_code == 200
    # Later heir retires in favour of other: other owns old and heir (a chain).
    assert (await w.retire("heir", "other")).status_code == 200
    whoami = (await w.client.get("/v1/whoami", headers=w.h("other"))).json()
    assert whoami["inherits_from"] == ["heir", "old"]
    assert await w.status("other", a_note["id"]) == 200
    assert await w.status("other", b_note["id"]) == 200
    assert await w.status("peer", a_note["id"]) == 404
    # A cycle (old back to active behind the guard, then heir -> old) is refused.
    conn = await asyncpg.connect(w.dsn)
    try:
        await conn.execute("UPDATE agents SET status = 'active' WHERE name IN ('old', 'heir')")
    finally:
        await conn.close()
    with pytest.raises(SuccessionRefused, match="cycle"):
        await w.store.retire_agent("other", "old", "x")
    refused = await w.client.post(
        "/v1/admin/agents/other/retire",
        json={"successor": "old", "dry_run": False},
        headers=w.admin,
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "succession_refused"


async def test_a_retired_agent_cannot_be_activated_again_until_undone(world) -> None:
    w = world
    assert (await w.retire()).status_code == 200
    body = {"trust_level": 2, "home_fleet_id": w.fleets["red"]}
    refused = await w.client.post("/v1/admin/agents/old/activate", json=body, headers=w.admin)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "agent_retired"
    with pytest.raises(AgentRetired):
        await w.store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id="x")
    assert (
        await w.client.post("/v1/admin/successions/old/undo", headers=w.admin)
    ).status_code == 200
    ok = await w.client.post("/v1/admin/agents/old/activate", json=body, headers=w.admin)
    assert ok.status_code == 200 and ok.json()["key"]  # a new key, as for any revoked agent


async def test_pins_inherited_count_toward_the_cap_on_postgres(world) -> None:
    w = world
    await w.store.register_agent("heir2", "owner")
    access = AccessService(w.store, w.auth)
    admin = await w.auth.verify(w.admin_key)
    _, key = await access.activate("heir2", TrustLevel.CONTRIBUTOR, w.fleets["red"], admin)
    w.keys["heir2"] = key
    facts = [await w.write("old", f"fact {i}", scope="fleet") for i in range(5)]
    for f in facts[:3]:
        assert (
            await w.client.put(f"/v1/entries/{f['id']}/pin", headers=w.h("old"))
        ).status_code == 200
    assert (await w.retire("old", "heir2")).status_code == 200
    assert len(await w.store.list_pins(w.fleets["red"])) == 3  # none dropped
    full = await w.client.put(f"/v1/entries/{facts[3]['id']}/pin", headers=w.h("heir2"))
    assert full.status_code == 422 and "3 of this fleet's pins" in full.text
    assert (
        await w.client.put(f"/v1/entries/{facts[3]['id']}/pin", headers=w.h("peer"))
    ).status_code == 200
    # The successor unpins an inherited pin and may pin again.
    assert (
        await w.client.delete(f"/v1/entries/{facts[0]['id']}/pin", headers=w.h("heir2"))
    ).status_code == 200
    assert (
        await w.client.put(f"/v1/entries/{facts[4]['id']}/pin", headers=w.h("heir2"))
    ).status_code == 200


async def test_two_retirements_to_the_same_agent_race_to_one(world) -> None:
    w = world
    results = await asyncio.gather(
        w.store.retire_agent("old", "heir", "a"),
        w.store.retire_agent("old", "other", "b"),
        return_exceptions=True,
    )
    assert sum(isinstance(r, SuccessionRefused) for r in results) == 1
    assert len(await w.store.list_successions()) == 1


async def test_the_key_check_uses_one_query(world) -> None:
    """The chain rides the key lookup: one round trip, no extra query."""
    w = world
    await w.retire("old", "heir")
    await w.retire("heir", "other")
    pool = await w.auth._ensure_pool()
    seen: list[str] = []
    real = asyncpg.Connection.fetchrow

    async def spy(self, query, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(query)
        return await real(self, query, *args, **kwargs)

    asyncpg.Connection.fetchrow = spy  # type: ignore[method-assign]
    try:
        cred = await w.auth.verify(w.keys["other"])
    finally:
        asyncpg.Connection.fetchrow = real  # type: ignore[method-assign]
    assert cred is not None and cred.inherits_from == ("heir", "old")
    assert cred.owned_authors == ("other", "heir", "old")
    assert len(seen) == 1 and "successions" in seen[0]
    assert pool is not None


async def test_opposite_retirements_race_to_exactly_one_winner(world) -> None:
    """A to B and B to A at once: the agent rows are locked in name order, so one
    wins and the other is refused (the successor is no longer active), never both."""
    w = world
    for _ in range(5):
        results = await asyncio.gather(
            w.client.post(
                "/v1/admin/agents/old/retire",
                json={"successor": "heir", "dry_run": False},
                headers=w.admin,
            ),
            w.client.post(
                "/v1/admin/agents/heir/retire",
                json={"successor": "old", "dry_run": False},
                headers=w.admin,
            ),
        )
        codes = sorted(r.status_code for r in results)
        assert codes == [200, 409], [r.text for r in results]
        assert len(await w.store.list_successions()) == 1
        # Reset for the next round.
        await w.store.undo_succession((await w.store.list_successions())[0].retired_agent)
        conn = await asyncpg.connect(w.dsn)
        try:
            await conn.execute("UPDATE agents SET status = 'active' WHERE name IN ('old', 'heir')")
        finally:
            await conn.close()


async def test_a_retirement_racing_an_activation_never_leaves_an_active_retired_agent(
    world,
) -> None:
    w = world
    await w.app.access_service.revoke("old", await w.auth.verify(w.admin_key))
    body = {"trust_level": 2, "home_fleet_id": w.fleets["red"]}
    retire, activate = await asyncio.gather(
        w.retire("old", "heir"),
        w.client.post("/v1/admin/agents/old/activate", json=body, headers=w.admin),
    )
    status = (await w.store.get_agent("old")).status
    standing = await w.store.get_succession("old")
    if standing is not None:
        assert status is AgentStatus.REVOKED  # retired agents are never active
        assert activate.status_code in (409,)
    else:  # activation won the lock first; the retirement then saw an active agent
        assert activate.status_code == 200 and status is AgentStatus.ACTIVE
        assert retire.status_code in (200, 409)
