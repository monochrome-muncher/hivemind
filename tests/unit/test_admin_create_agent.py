"""Admin-created agents (ADR 0120): the admin registers and activates an
agent for its owner in one call and gets the key once.

Pinned at the service seam (``AccessService.create_agent``) and at the
REST surface (``POST /v1/admin/agents``).
"""

from __future__ import annotations

import pytest

from hivemind.domain.access import AgentStatus, FleetMerged, TrustLevel, UnknownFleet
from hivemind.domain.audit import AuditFilters
from hivemind.domain.validation import InvalidInput
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService, NameTaken
from hivemind.services.errors import PermissionDenied
from tests.fakes import FakeAuthenticator, make_store
from tests.unit.test_api_endpoints import make_client, make_hivemind_app


def admin() -> Credential:
    return Credential(user_id="admin", is_admin=True)


def org() -> Credential:
    return Credential(user_id="org", is_org=True, access_controlled=True)


async def _service() -> tuple[AccessService, FakeAuthenticator, MemoryStore, str]:
    store = make_store()
    auth = FakeAuthenticator(store=store)
    fleet = await store.create_fleet("data-eng")
    return AccessService(store, auth), auth, store, fleet.id


async def test_creates_an_active_agent_and_returns_a_working_key() -> None:
    service, auth, store, fleet_id = await _service()
    agent, key = await service.create_agent(
        "jane-laptop", "jane", TrustLevel.CONTRIBUTOR, fleet_id, admin()
    )
    assert agent.status is AgentStatus.ACTIVE
    assert agent.trust_level is TrustLevel.CONTRIBUTOR
    assert agent.home_fleet_id == fleet_id
    assert agent.owner_alias == "jane"
    credential = await auth.verify(key)
    assert credential is not None and credential.agent_name == "jane-laptop"
    stored = await store.get_agent("jane-laptop")
    assert stored is not None and stored.status is AgentStatus.ACTIVE


async def test_audits_the_registration_and_the_activation_without_the_key() -> None:
    service, _, store, fleet_id = await _service()
    _, key = await service.create_agent("jane-laptop", "jane", TrustLevel.LURKER, fleet_id, admin())
    rows = await store.list_audit(AuditFilters(), limit=100)
    actions = sorted(r.action.value for r in rows)
    assert actions == ["agent.activate", "agent.register"]
    assert all(r.actor_kind.value == "admin_key" and r.target == "jane-laptop" for r in rows)
    assert key not in repr(rows)


@pytest.mark.parametrize("credential", [org(), Credential(user_id="a", agent_name="a")])
async def test_only_the_admin_key_may_create_an_agent(credential) -> None:
    service, _, store, fleet_id = await _service()
    with pytest.raises(PermissionDenied):
        await service.create_agent("jane-laptop", "jane", TrustLevel.LURKER, fleet_id, credential)
    assert await store.get_agent("jane-laptop") is None


@pytest.mark.parametrize("alias", ["", "   "])
async def test_the_owner_alias_is_required(alias) -> None:
    service, _, store, fleet_id = await _service()
    with pytest.raises(InvalidInput):
        await service.create_agent("jane-laptop", alias, TrustLevel.LURKER, fleet_id, admin())
    assert await store.get_agent("jane-laptop") is None


async def test_a_bad_name_or_unknown_fleet_leaves_no_record() -> None:
    service, _, store, fleet_id = await _service()
    with pytest.raises(InvalidInput):
        await service.create_agent("admin", "jane", TrustLevel.LURKER, fleet_id, admin())
    with pytest.raises(UnknownFleet):
        await service.create_agent("jane-laptop", "jane", TrustLevel.LURKER, "nope", admin())
    assert await store.list_agents() == []


async def test_a_merged_fleet_is_refused_before_any_record() -> None:
    service, _, store, fleet_id = await _service()
    other = await store.create_fleet("old-ops")
    await service.merges.merge(other.id, admin(), target_fleet_id=fleet_id)
    with pytest.raises(FleetMerged):
        await service.create_agent("jane-laptop", "jane", TrustLevel.LURKER, other.id, admin())
    assert await store.get_agent("jane-laptop") is None


@pytest.mark.parametrize("status", ["pending", "active", "revoked"])
async def test_an_existing_name_is_refused_and_left_alone(status) -> None:
    service, _, store, fleet_id = await _service()
    await service.register("jane-laptop", org(), owner_alias="jane")
    if status == "active":
        await service.activate("jane-laptop", TrustLevel.LURKER, fleet_id, admin())
    elif status == "revoked":
        await service.revoke("jane-laptop", admin())
    before = await store.get_agent("jane-laptop")
    with pytest.raises(NameTaken) as excinfo:
        await service.create_agent("jane-laptop", "jane", TrustLevel.PRIVILEGED, fleet_id, admin())
    assert status in str(excinfo.value)
    assert await store.get_agent("jane-laptop") == before


async def test_a_case_variant_of_an_existing_name_is_refused() -> None:
    service, _, store, fleet_id = await _service()
    await service.register("Jane-Laptop", org(), owner_alias="someone")
    with pytest.raises(NameTaken):
        await service.create_agent("jane-laptop", "jane", TrustLevel.LURKER, fleet_id, admin())
    agents = await store.list_agents()
    assert [(a.name, a.status) for a in agents] == [("Jane-Laptop", AgentStatus.PENDING)]


async def test_dev_mode_without_an_authenticator_writes_nothing() -> None:
    store = make_store()
    fleet = await store.create_fleet("data-eng")
    service = AccessService(store, None)
    with pytest.raises(PermissionDenied):
        await service.create_agent("jane-laptop", "jane", TrustLevel.LURKER, fleet.id, admin())
    assert await store.get_agent("jane-laptop") is None


# -- REST: POST /v1/admin/agents ---------------------------------------------


async def test_rest_creates_the_agent_and_returns_the_key_once() -> None:
    app = make_hivemind_app()
    fleet = await app.store.create_fleet("data-eng")
    body = {
        "name": "jane-laptop",
        "owner_alias": "jane",
        "trust_level": 2,
        "home_fleet_id": fleet.id,
    }
    async with make_client(app) as client:
        resp = await client.post("/v1/admin/agents", json=body, headers={"X-API-Key": "key-admin"})
        assert resp.status_code == 201
        out = resp.json()
        assert (out["name"], out["status"], out["trust_level"], out["owner_alias"]) == (
            "jane-laptop",
            "active",
            2,
            "jane",
        )
        assert out["home_fleet_id"] == fleet.id
        who = await client.get("/v1/whoami", headers={"X-API-Key": out["key"]})
        assert who.status_code == 200 and who.json()["name"] == "jane-laptop"
        again = await client.post("/v1/admin/agents", json=body, headers={"X-API-Key": "key-admin"})
        assert again.status_code == 409
        assert again.json()["error"]["code"] == "name_conflict"
        assert "key" not in again.json()


async def test_rest_refusals() -> None:
    app = make_hivemind_app()
    fleet = await app.store.create_fleet("data-eng")
    body = {"name": "jane-laptop", "owner_alias": "jane", "home_fleet_id": fleet.id}
    async with make_client(app) as client:
        as_org = await client.post("/v1/admin/agents", json=body, headers={"X-API-Key": "key-org"})
        as_agent = await client.post(
            "/v1/admin/agents", json=body, headers={"X-API-Key": "key-alice"}
        )
        no_alias = await client.post(
            "/v1/admin/agents",
            json={"name": "jane-laptop", "home_fleet_id": fleet.id},
            headers={"X-API-Key": "key-admin"},
        )
        bad_level = await client.post(
            "/v1/admin/agents", json={**body, "trust_level": 4}, headers={"X-API-Key": "key-admin"}
        )
        unknown_fleet = await client.post(
            "/v1/admin/agents",
            json={**body, "home_fleet_id": "nope"},
            headers={"X-API-Key": "key-admin"},
        )
    assert as_org.status_code == as_agent.status_code == 403
    assert no_alias.status_code == bad_level.status_code == 422
    assert (unknown_fleet.status_code, unknown_fleet.json()["error"]["code"]) == (404, "not_found")
    assert await app.store.get_agent("jane-laptop") is None


async def test_a_name_registered_between_the_check_and_the_insert_is_refused() -> None:
    service, auth, store, fleet_id = await _service()
    real_get = store.get_agent
    calls = 0

    async def racing_get(name: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:  # create_agent's own check sees nothing...
            await store.register_agent(name, "jane")  # ...then the owner registers
            return None
        return await real_get(name)

    store.get_agent = racing_get  # type: ignore[method-assign]
    with pytest.raises(NameTaken):
        await service.create_agent("jane-laptop", "jane", TrustLevel.LURKER, fleet_id, admin())
    agent = await real_get("jane-laptop")
    assert agent is not None and agent.status is AgentStatus.PENDING
    assert "jane-laptop" not in auth._issued_agent_keys
