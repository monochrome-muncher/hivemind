"""An admin renames an agent (ADR 0159): the service, the REST route and the history mapping."""

from __future__ import annotations

import pytest

from hivemind.domain.access import AgentStatus, TrustLevel, UnknownAgent
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters, AuditRecord
from hivemind.domain.history import HistoryContext, describe
from hivemind.domain.validation import InvalidInput, utcnow
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.errors import NameTaken, PermissionDenied
from hivemind.services.successions import SuccessionService
from tests.fakes import FakeAuthenticator, make_clock, make_store_client

ADMIN = Credential(user_id="admin", is_admin=True, key_id="abcdef012345")
ORG = Credential(user_id="org", is_org=True, access_controlled=True)
AGENT = Credential(user_id="alice", agent_name="alice", access_controlled=True)
ADMIN_H = {"X-API-Key": "hm_admin"}
ORG_H = {"X-API-Key": "hm_org"}


class World:
    def __init__(self) -> None:
        self.store = MemoryStore(make_clock())
        self.access = AccessService(self.store, FakeAuthenticator(store=self.store))
        self.successions = SuccessionService(self.store, self.access.require_authenticator)
        self.fleet = ""

    async def agent(self, name: str, *, active: bool = True) -> None:
        if not self.fleet:
            self.fleet = (await self.access.create_fleet("platform", ADMIN)).id
        await self.store.register_agent(name, "owner")
        if active:
            await self.store.activate_agent(
                name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=self.fleet
            )

    async def renames(self) -> list[tuple[str | None, dict[str, object], ActorKind]]:
        rows = await self.store.list_audit(AuditFilters(action=AuditAction.AGENT_RENAME), 50)
        return [(r.target, dict(r.detail), r.actor_kind) for r in reversed(rows)]


# -- the service --------------------------------------------------------------------


async def test_the_admin_renames_an_agent_and_the_target_is_the_new_name() -> None:
    world = World()
    await world.agent("alice")
    agent = await world.access.rename_agent("alice", "alice-2", ADMIN)
    assert (agent.name, agent.status) == ("alice-2", AgentStatus.ACTIVE)
    assert await world.store.get_agent("alice") is None
    assert await world.renames() == [
        ("alice-2", {"from": "alice", "to": "alice-2"}, ActorKind.ADMIN_KEY)
    ]


@pytest.mark.parametrize("status", ["pending", "active", "revoked"])
async def test_any_status_may_be_renamed(status: str) -> None:
    world = World()
    await world.agent("alice", active=status != "pending")
    if status == "revoked":
        await world.access.revoke("alice", ADMIN)
    renamed = await world.access.rename_agent("alice", "alice-2", ADMIN)
    assert renamed.status.value == status


async def test_a_retired_agent_may_be_renamed() -> None:
    world = World()
    await world.agent("alice")
    await world.agent("bob")
    await world.successions.retire("alice", "bob", ADMIN)
    await world.access.rename_agent("alice", "alice-old", ADMIN)
    await world.access.rename_agent("bob", "robert", ADMIN)
    assert await world.store.inherited_authors("robert") == ("alice-old",)
    succession = await world.store.get_succession("alice-old")
    assert succession is not None and succession.successor == "robert"


async def test_the_same_name_changes_and_audits_nothing_but_a_case_change_is_a_rename() -> None:
    world = World()
    await world.agent("bob")
    same = await world.access.rename_agent("bob", "bob", ADMIN)
    assert same.name == "bob" and await world.renames() == []
    cased = await world.access.rename_agent("bob", "Bob", ADMIN)
    assert cased.name == "Bob"
    assert await world.renames() == [("Bob", {"from": "bob", "to": "Bob"}, ActorKind.ADMIN_KEY)]


@pytest.mark.parametrize("who", [ORG, AGENT])
async def test_only_the_admin_key_may_rename_and_nothing_is_looked_up_first(
    who: Credential,
) -> None:
    world = World()
    await world.agent("alice")
    for target in ("alice", "ghost"):
        with pytest.raises(PermissionDenied):
            await world.access.rename_agent(target, "alice-2", who)
    assert await world.store.get_agent("alice") is not None
    assert await world.renames() == []


@pytest.mark.parametrize(
    "bad", ["", "has space", "a" * 64, "-leading", "tab\there", "admin", "ORG", "zero​width"]
)
async def test_the_new_name_is_validated_as_a_registration_name(bad: str) -> None:
    world = World()
    await world.agent("alice")
    with pytest.raises(InvalidInput):
        await world.access.rename_agent("alice", bad, ADMIN)
    assert await world.store.get_agent("alice") is not None
    assert await world.renames() == []


async def test_an_unknown_agent_is_refused() -> None:
    world = World()
    with pytest.raises(UnknownAgent):
        await world.access.rename_agent("ghost", "casper", ADMIN)
    assert await world.renames() == []


async def test_a_name_another_agent_holds_in_any_case_is_taken() -> None:
    world = World()
    await world.agent("alice")
    await world.agent("bob")
    for taken in ("bob", "BOB"):
        with pytest.raises(NameTaken):
            await world.access.rename_agent("alice", taken, ADMIN)
    assert await world.renames() == []


async def test_a_name_another_agent_left_is_taken_but_its_own_is_not() -> None:
    world = World()
    await world.agent("alice")
    await world.agent("bob")
    await world.access.rename_agent("alice", "carol", ADMIN)
    for left in ("alice", "ALICE"):
        with pytest.raises(NameTaken):
            await world.access.rename_agent("bob", left, ADMIN)
    back = await world.access.rename_agent("carol", "alice", ADMIN)
    assert back.name == "alice"
    assert await world.store.former_agent_names(["alice"]) == {"alice": ("carol",)}
    assert [t for t, _, _ in await world.renames()] == ["carol", "alice"]


async def test_a_name_an_agent_left_cannot_be_registered_by_anyone() -> None:
    world = World()
    await world.agent("alice")
    await world.access.rename_agent("alice", "carol", ADMIN)
    for name in ("alice", "Alice"):
        with pytest.raises(NameTaken):
            await world.access.register(name, ORG, "owner")
        with pytest.raises(NameTaken):
            await world.access.register(name, ADMIN, "owner")
        with pytest.raises(NameTaken):
            await world.access.create_agent(name, "owner", TrustLevel.LURKER, world.fleet, ADMIN)
    assert [a.name for a in await world.store.list_agents()] == ["carol"]


async def test_the_rename_drops_the_cached_figures_keyed_by_name() -> None:
    world = World()
    await world.agent("alice")
    dropped: list[str] = []
    world.access.on_agent_rename.append(lambda: dropped.append("dropped"))
    await world.access.rename_agent("alice", "alice", ADMIN)
    assert dropped == []
    await world.access.rename_agent("alice", "alice-2", ADMIN)
    assert dropped == ["dropped"]


# -- the history follows the former names -------------------------------------------


async def test_the_audit_log_subject_includes_the_rows_under_the_former_names() -> None:
    world = World()
    await world.agent("alice")
    await world.agent("bob")
    await world.access.set_owner_alias("alice", "jane", ADMIN)
    await world.access.rename_agent("alice", "carol", ADMIN)
    await world.access.set_owner_alias("carol", "jane.d", ADMIN)
    await world.access.rename_agent("carol", "dora", ADMIN)

    rows = await world.access.list_audit(ADMIN, AuditFilters(subjects=("dora",)), 50)
    assert [(r.action.value, r.target) for r in reversed(rows)] == [
        ("agent.owner_set", "alice"),
        ("agent.rename", "carol"),
        ("agent.owner_set", "carol"),
        ("agent.rename", "dora"),
    ]
    other = await world.access.list_audit(ADMIN, AuditFilters(subjects=("bob",)), 50)
    assert all(r.target != "alice" for r in other)


def test_the_portal_history_says_what_was_renamed_and_calls_its_former_names_this_agent() -> None:
    ctx = HistoryContext(
        me="dora",
        former=("alice", "carol"),
        inherited=(),
        fleet_names={},
        readable_entries=frozenset(),
    )
    assert ctx.agent("alice") == "this agent" and ctx.agent("dora") == "this agent"
    assert ctx.agent("bob") == "another agent"
    record = AuditRecord(
        id="1",
        occurred_at=utcnow(),
        actor_kind=ActorKind.ADMIN_KEY,
        actor="admin:abc",
        action=AuditAction.AGENT_RENAME,
        target="dora",
        detail={"from": "carol", "to": "dora"},
    )
    item = describe(record, ctx)
    assert (item.summary, item.by, item.about) == ("Renamed from carol to dora", "admin", "dora")


# -- the REST route -----------------------------------------------------------------


async def _client_world() -> tuple[MemoryStore, World]:
    world = World()
    await world.agent("alice")
    await world.agent("bob")
    return world.store, world


def _rename(client, name: str, new: str, headers: dict[str, str] = ADMIN_H):
    return client.post(f"/v1/admin/agents/{name}/rename", json={"name": new}, headers=headers)


async def test_rest_renames_and_answers_the_renamed_agent() -> None:
    store, _ = await _client_world()
    async with make_store_client(store) as client:
        resp = await _rename(client, "alice", "alice-2")
        listed = await client.get("/v1/admin/agents", headers=ADMIN_H)
    assert resp.status_code == 200, resp.text
    assert (resp.json()["name"], resp.json()["status"]) == ("alice-2", "active")
    assert [a["name"] for a in listed.json()] == ["alice-2", "bob"]
    rows = await store.list_audit(AuditFilters(action=AuditAction.AGENT_RENAME), 5)
    assert [(r.target, dict(r.detail), r.actor_kind) for r in rows] == [
        ("alice-2", {"from": "alice", "to": "alice-2"}, ActorKind.ADMIN_KEY)
    ]


async def test_rest_refusals() -> None:
    store, _ = await _client_world()
    async with make_store_client(store, {"k": AGENT}) as client:
        for headers in ({"X-API-Key": "k"}, ORG_H):
            forbidden = await _rename(client, "ghost", "x", headers)
            assert forbidden.status_code == 403  # before any lookup: no 404 for a non-admin
            assert forbidden.json()["error"]["code"] == "forbidden"
        unknown = await _rename(client, "ghost", "casper")
        assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "not_found")
        current = await _rename(client, "alice", "BOB")
        assert (current.status_code, current.json()["error"]["code"]) == (409, "name_conflict")
        invalid = await _rename(client, "alice", "admin")
        assert (invalid.status_code, invalid.json()["error"]["code"]) == (422, "invalid_input")
        await _rename(client, "alice", "carol")
        former = await _rename(client, "bob", "alice")
        assert (former.status_code, former.json()["error"]["code"]) == (409, "name_conflict")
        registered = await client.post(
            "/v1/agents", json={"name": "ALICE", "owner_alias": "x"}, headers=ORG_H
        )
        assert (registered.status_code, registered.json()["error"]["code"]) == (
            409,
            "name_conflict",
        )
    assert [a.name for a in await store.list_agents()] == ["bob", "carol"]


async def test_rest_the_same_name_is_a_200_with_no_audit_row() -> None:
    store, _ = await _client_world()
    async with make_store_client(store) as client:
        resp = await _rename(client, "alice", "alice")
    assert resp.status_code == 200 and resp.json()["name"] == "alice"
    assert await store.list_audit(AuditFilters(action=AuditAction.AGENT_RENAME), 5) == []


async def test_rest_the_audit_filter_lists_the_action() -> None:
    store, _ = await _client_world()
    async with make_store_client(store) as client:
        await _rename(client, "alice", "alice-2")
        log = await client.get(
            "/v1/admin/audit-log", params={"action": "agent.rename"}, headers=ADMIN_H
        )
    assert [(r["action"], r["target"]) for r in log.json()] == [("agent.rename", "alice-2")]
