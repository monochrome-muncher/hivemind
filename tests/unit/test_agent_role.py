"""Agent roles (ADR 0144): the hint text, the admin service and routes, the ``role`` and
``role_hint`` in ``hive_whoami``, and the usage report and Prometheus gauges."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from prometheus_client.parser import text_string_to_metric_families

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, InvalidAgentStatus, TrustLevel, UnknownAgent
from hivemind.domain.agent_role import AgentRole, role_hint
from hivemind.domain.audit import AuditFilters
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.health import RoleActivity
from hivemind.mcp.app import McpHivemind, hive_whoami
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService, PermissionDenied
from hivemind.services.governance import GovernanceService
from hivemind.services.metrics import MetricsService
from hivemind.services.read import ReadService
from hivemind.services.search import SearchService
from hivemind.services.write import WriteService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config

ADMIN = Credential(user_id="admin", is_admin=True, key_id="abcdef012345")
ORG = Credential(user_id="org", is_org=True, access_controlled=True)
ADMIN_H = {"X-API-Key": "hm_admin"}

SCOUT_HINT = (
    "You are a scout for platform: on low-stakes tasks, prefer the less-tested of two rivals, "
    "search with explore: true when results look settled, re-check old unconfirmed entries "
    "you rely on, and write what you tried, including dead ends. Your access is unchanged."
)


# -- the hint ---------------------------------------------------------------------


def test_the_scout_hint_is_fixed_text_with_the_fleet_name() -> None:
    assert role_hint(AgentRole.SCOUT, "platform") == SCOUT_HINT
    assert role_hint(AgentRole.SCOUT, None).startswith("You are a scout for your fleet: on low-")


# -- the admin service ------------------------------------------------------------


class World:
    def __init__(self) -> None:
        self.store = MemoryStore(make_clock())
        self.access = AccessService(self.store, FakeAuthenticator())

    async def agent(self, name: str = "alice", fleet: str = "platform") -> str:
        fleet_id = (await self.access.create_fleet(fleet, ADMIN)).id
        await self.store.register_agent(name, "owner")
        await self.store.activate_agent(
            name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet_id
        )
        return fleet_id

    async def audit(self) -> list[tuple[str, str | None, dict[str, object]]]:
        rows = await self.store.list_audit(AuditFilters(), 50)
        return [
            (r.action.value, r.target, dict(r.detail)) for r in reversed(rows) if "role" in r.action
        ]


async def test_the_admin_marks_a_scout_and_it_is_audited() -> None:
    world = World()
    await world.agent()
    agent = await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    assert agent.role is AgentRole.SCOUT
    assert (await world.store.get_agent("alice")).role is AgentRole.SCOUT  # type: ignore[union-attr]
    assert await world.audit() == [("agent.role_set", "alice", {"role": "scout"})]


async def test_marking_a_scout_again_changes_and_audits_nothing() -> None:
    world = World()
    await world.agent()
    await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    assert len(await world.audit()) == 1


async def test_clearing_audits_the_role_it_ended_and_clearing_none_audits_nothing() -> None:
    world = World()
    await world.agent()
    await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    cleared = await world.access.clear_agent_role("alice", ADMIN)
    assert cleared.role is None
    again = await world.access.clear_agent_role("alice", ADMIN)
    assert again.role is None
    assert await world.audit() == [
        ("agent.role_set", "alice", {"role": "scout"}),
        ("agent.role_clear", "alice", {"role": "scout"}),
    ]


async def test_a_pending_agent_may_be_marked() -> None:
    world = World()
    await world.store.register_agent("alice", "owner")
    agent = await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    assert (agent.status, agent.role) == (AgentStatus.PENDING, AgentRole.SCOUT)


async def test_a_revoked_agent_cannot_be_made_a_scout() -> None:
    world = World()
    await world.agent()
    await world.access.revoke("alice", ADMIN)
    with pytest.raises(InvalidAgentStatus):
        await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    assert (await world.store.get_agent("alice")).role is None  # type: ignore[union-attr]
    assert await world.audit() == []


async def test_a_scout_that_is_revoked_can_still_be_cleared() -> None:
    world = World()
    await world.agent()
    await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    await world.access.revoke("alice", ADMIN)
    assert (await world.access.clear_agent_role("alice", ADMIN)).role is None


async def test_an_unknown_agent_is_refused() -> None:
    world = World()
    with pytest.raises(UnknownAgent):
        await world.access.set_agent_role("ghost", AgentRole.SCOUT, ADMIN)
    with pytest.raises(UnknownAgent):
        await world.access.clear_agent_role("ghost", ADMIN)
    assert await world.audit() == []


@pytest.mark.parametrize(
    "who", [ORG, Credential(user_id="a", agent_name="a", access_controlled=True)]
)
async def test_only_the_admin_key_may_set_or_clear(who: Credential) -> None:
    world = World()
    await world.agent()
    with pytest.raises(PermissionDenied):
        await world.access.set_agent_role("alice", AgentRole.SCOUT, who)
    with pytest.raises(PermissionDenied):
        await world.access.clear_agent_role("alice", who)
    assert (await world.store.get_agent("alice")).role is None  # type: ignore[union-attr]
    assert await world.audit() == []


# -- a role stays with its agent --------------------------------------------------


async def test_a_role_survives_a_move_and_a_merge_but_a_successor_does_not_inherit_it() -> None:
    world = World()
    blue = await world.agent("alice", "blue")
    red = (await world.access.create_fleet("red", ADMIN)).id
    await world.store.register_agent("bob", "owner")
    await world.store.activate_agent("bob", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=red)
    await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)

    await world.access.merges.move(blue, ["alice"], ADMIN, target_fleet_id=red)
    assert (await world.store.get_agent("alice")).role is AgentRole.SCOUT  # type: ignore[union-attr]
    await world.access.merges.merge(red, ADMIN, target_fleet_id=blue)
    assert (await world.store.get_agent("alice")).role is AgentRole.SCOUT  # type: ignore[union-attr]
    assert (await world.store.get_agent("bob")).role is None  # type: ignore[union-attr]

    await world.access.successions.retire("alice", "bob", ADMIN)
    retired = await world.store.get_agent("alice")
    successor = await world.store.get_agent("bob")
    assert retired is not None and successor is not None
    assert (retired.status, retired.role) == (AgentStatus.REVOKED, AgentRole.SCOUT)
    assert successor.role is None


# -- hive_whoami ------------------------------------------------------------------


def _agent(fleet_id: str | None) -> Credential:
    return Credential(
        user_id="alice",
        agent_id="alice",
        agent_name="alice",
        access_controlled=True,
        trust_level=TrustLevel.CONTRIBUTOR,
        home_fleet_id=fleet_id,
    )


def _surface(world: World, credential: Credential) -> McpHivemind:
    clock, embedder, config = make_clock(), make_embedder(), make_search_config()
    governance = GovernanceService(world.store)
    return McpHivemind(
        store=world.store,
        write_service=WriteService(world.store, embedder),
        search_service=SearchService(world.store, embedder, config, now_fn=clock),
        governance_service=governance,
        read_service=ReadService(world.store, governance, config),
        access_service=world.access,
        search_config=config,
        credential=credential,
    )


async def test_whoami_carries_the_role_and_its_hint_only_for_a_scout() -> None:
    world = World()
    fleet_id = await world.agent()
    alice = _agent(fleet_id)
    plain = await hive_whoami(_surface(world, alice))
    assert "role" not in plain and "role_hint" not in plain

    await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    scout = await hive_whoami(_surface(world, alice))
    assert (scout["role"], scout["role_hint"]) == ("scout", SCOUT_HINT)
    assert {k: v for k, v in scout.items() if k not in ("role", "role_hint")} == plain

    await world.access.clear_agent_role("alice", ADMIN)
    assert "role" not in await hive_whoami(_surface(world, alice))


async def test_the_hint_names_your_fleet_for_an_agent_without_one() -> None:
    world = World()
    await world.agent()
    await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    scout = await hive_whoami(_surface(world, _agent(None)))
    assert str(scout["role_hint"]).startswith("You are a scout for your fleet: on low-")


async def test_other_keys_get_no_role_even_when_named_like_a_scout() -> None:
    world = World()
    await world.agent()
    await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    for key in (ADMIN, ORG):
        assert "role" not in await hive_whoami(_surface(world, key))


async def test_a_role_changes_nothing_an_agent_may_read_or_write() -> None:
    world = World()
    fleet_id = await world.agent()
    alice = _agent(fleet_id)
    before = await hive_whoami(_surface(world, alice))
    await world.access.set_agent_role("alice", AgentRole.SCOUT, ADMIN)
    after = await hive_whoami(_surface(world, alice))
    for field in ("can_read", "can_write_scopes", "trust_level", "home_fleet_id"):
        assert after[field] == before[field]


# -- the REST routes --------------------------------------------------------------


def _client(store: MemoryStore, agents: dict[str, Credential] | None = None) -> httpx.AsyncClient:
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(agent_credentials=agents),
        search_config=make_search_config(),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://t"
    )


async def _activated(store: MemoryStore, name: str = "alice") -> str:
    fleet = await store.create_fleet("platform")
    await store.register_agent(name, "owner")
    await store.activate_agent(name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id)
    return fleet.id


async def test_rest_sets_and_clears_a_role_and_the_agent_list_shows_it() -> None:
    store = MemoryStore(make_clock())
    await _activated(store)
    async with _client(store) as client:
        put = await client.put(
            "/v1/admin/agents/alice/role", json={"role": "scout"}, headers=ADMIN_H
        )
        listed = await client.get("/v1/admin/agents", headers=ADMIN_H)
        cleared = await client.delete("/v1/admin/agents/alice/role", headers=ADMIN_H)
        again = await client.delete("/v1/admin/agents/alice/role", headers=ADMIN_H)
        after = await client.get("/v1/admin/agents", headers=ADMIN_H)
    assert put.status_code == 200
    assert (put.json()["name"], put.json()["role"]) == ("alice", "scout")
    assert listed.json()[0]["role"] == "scout"
    assert (cleared.status_code, cleared.json()["role"]) == (200, None)
    assert (again.status_code, again.json()["role"]) == (200, None)
    assert after.json()[0]["role"] is None
    rows = await store.list_audit(AuditFilters(), 10)
    assert sorted(r.action.value for r in rows if "role" in r.action) == [
        "agent.role_clear",
        "agent.role_set",
    ]


@pytest.mark.parametrize("body", [{"role": "queen"}, {"role": None}, {}])
async def test_rest_refuses_an_unknown_role_with_422(body: dict[str, object]) -> None:
    store = MemoryStore(make_clock())
    await _activated(store)
    async with _client(store) as client:
        resp = await client.put("/v1/admin/agents/alice/role", json=body, headers=ADMIN_H)
    assert resp.status_code == 422
    assert (await store.get_agent("alice")).role is None  # type: ignore[union-attr]


async def test_rest_errors_for_unknown_revoked_and_unauthorised() -> None:
    store = MemoryStore(make_clock())
    await _activated(store)
    await _activated_revoked(store)
    dev = Credential(user_id="dev", agent_name="dev", access_controlled=True)
    body = {"role": "scout"}
    async with _client(store, {"dev-key": dev}) as client:
        unknown = await client.put("/v1/admin/agents/nobody/role", json=body, headers=ADMIN_H)
        unknown_clear = await client.delete("/v1/admin/agents/nobody/role", headers=ADMIN_H)
        revoked = await client.put("/v1/admin/agents/gone/role", json=body, headers=ADMIN_H)
        agent = await client.put(
            "/v1/admin/agents/alice/role", json=body, headers={"X-API-Key": "dev-key"}
        )
        agent_clear = await client.delete(
            "/v1/admin/agents/alice/role", headers={"X-API-Key": "dev-key"}
        )
        anonymous = await client.put("/v1/admin/agents/alice/role", json=body)
    assert (unknown.status_code, unknown_clear.status_code) == (404, 404)
    assert (revoked.status_code, revoked.json()["error"]["code"]) == (409, "invalid_status")
    assert (agent.status_code, agent_clear.status_code, anonymous.status_code) == (403, 403, 401)


async def _activated_revoked(store: MemoryStore) -> None:
    await store.register_agent("gone", "owner")
    await store.revoke_agent("gone")


async def test_rest_whoami_and_mcp_whoami_agree() -> None:
    world = World()
    fleet_id = await world.agent()
    alice = _agent(fleet_id)
    async with _client(world.store, {"alice-key": alice}) as client:
        bare = (await client.get("/v1/whoami", headers={"X-API-Key": "alice-key"})).json()
        await client.put("/v1/admin/agents/alice/role", json={"role": "scout"}, headers=ADMIN_H)
        rest = (await client.get("/v1/whoami", headers={"X-API-Key": "alice-key"})).json()
    assert "role" not in bare and "role_hint" not in bare
    mcp = await hive_whoami(_surface(world, alice))
    assert (rest["role"], rest["role_hint"]) == ("scout", SCOUT_HINT)
    assert rest == mcp


# -- usage report and Prometheus --------------------------------------------------


async def test_the_usage_report_counts_roles_and_compares_scouts_with_the_rest() -> None:
    clock = make_clock(datetime.now(UTC))
    store = MemoryStore(clock)
    fleet = await store.create_fleet("platform")
    for name in ("alice", "bob", "carol", "dora"):
        await store.register_agent(name, "owner")
        await store.activate_agent(name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id)
    for name in ("alice", "bob", "carol"):
        await store.set_agent_role(name, AgentRole.SCOUT)
    await store.revoke_agent("carol")  # no longer counted as a scout in the roster

    async def write(author: str, tags: tuple[str, ...] = ()) -> str:
        summary = f"{author} {tags}"
        draft = EntryDraft(kind=Kind.FACT, summary=summary, author=author, agent=author, tags=tags)
        return (await store.create_entry(draft, [0.1, 0.1, 0.1, 0.1])).id

    first = await write("alice", ("dead-end",))
    await write("alice")
    plain = await write("dora", ("alternative",))
    await write("dora")
    await write("bob", ("alternative",))
    await store.record_feedback(Feedback(plain, "alice", "a", Verdict.HELPFUL, None, None))
    await store.record_feedback(Feedback(first, "dora", "d", Verdict.STALE, None, None))
    await store.record_feedback(Feedback(first, "carol", "c", Verdict.HELPFUL, None, None))

    report = await MetricsService(store, wall_clock=clock).usage_report()
    assert report.agents.by_role == {"scout": 2}
    assert report.health is not None
    assert report.health.role_activity_30d == {
        "scout": RoleActivity(
            writes=3, alternative_or_dead_end_writes=2, supersessions=0, verdicts=1
        ),
        "other": RoleActivity(
            writes=2, alternative_or_dead_end_writes=1, supersessions=0, verdicts=1
        ),
    }

    async with _client(store) as client:
        body = (await client.get("/v1/metrics", headers=ADMIN_H)).json()
        scrape = (await client.get("/metrics")).text
    assert body["agents"]["by_role"] == {"scout": 2}
    assert body["health"]["role_activity_30d"]["scout"] == {
        "writes": 3,
        "alternative_or_dead_end_writes": 2,
        "supersessions": 0,
        "verdicts": 1,
    }
    samples = {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(scrape)
        for s in family.samples
    }
    assert samples[("hivemind_agents_by_role", (("role", "scout"),))] == 2
    scout_writes = (("motion", "writes"), ("role", "scout"))
    assert samples[("hivemind_health_role_activity_30d", scout_writes)] == 3
    other_verdicts = (("motion", "verdicts"), ("role", "other"))
    assert samples[("hivemind_health_role_activity_30d", other_verdicts)] == 1


async def test_an_empty_pool_reports_no_scouts_and_no_activity() -> None:
    report = await MetricsService(MemoryStore(make_clock())).usage_report()
    assert report.agents.by_role == {"scout": 0}
    assert report.health is not None
    assert report.health.role_activity_30d == {"scout": RoleActivity(), "other": RoleActivity()}
