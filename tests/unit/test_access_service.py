"""Unit tests for the AccessService + write-scope resolution (ADRs 0011-0012).

These pin the access-plane behavior at the service seam (TDD): the
permission gates (which key may do what, ADR 0012), agent
registration/activation (the key is issued **once**), and write-scope
resolution (which scope a trust level may write, ADR 0011).
"""

from __future__ import annotations

import pytest

from hivemind.domain.access import AgentStatus, TrustLevel
from hivemind.ports import Credential
from hivemind.services.access import (
    AccessService,
    PermissionDenied,
    resolve_write_scope,
)
from tests.fakes import FakeAuthenticator, make_store


def org_credential() -> Credential:
    return Credential(user_id="org", is_org=True, access_controlled=True)


def admin_credential() -> Credential:
    return Credential(user_id="admin", is_admin=True)


def agent_credential(
    level: TrustLevel = TrustLevel.CONTRIBUTOR, home_fleet: str | None = "fleet-a"
) -> Credential:
    """An access-controlled agent credential at the given trust level."""
    return Credential(
        user_id="alice",
        agent_name="alice",
        access_controlled=True,
        trust_level=level,
        home_fleet_id=home_fleet,
    )


def legacy_credential() -> Credential:
    """A v1 credential (no access control: full read + any-scope write)."""
    return Credential(user_id="legacy", access_controlled=False)


def make_service() -> tuple[AccessService, FakeAuthenticator, object]:
    store = make_store()
    auth = FakeAuthenticator()
    return AccessService(store, auth), auth, store


# -- registration (gated: org or admin key, ADR 0012) ----------------------


async def test_register_with_org_key_creates_pending() -> None:
    service, _, store = make_service()
    agent = await service.register("alice", org_credential())
    assert agent.status is AgentStatus.PENDING
    assert agent.trust_level is TrustLevel.UNTRUSTED
    # The agent is persisted (pending, level 0, no fleet).
    stored = await store.get_agent("alice")
    assert stored is not None and stored.status is AgentStatus.PENDING


async def test_register_idempotent_for_pending() -> None:
    service, _, _ = make_service()
    first = await service.register("alice", org_credential())
    again = await service.register("alice", org_credential())
    assert again.name == first.name
    assert again.status is AgentStatus.PENDING


async def test_register_active_name_conflicts() -> None:
    service, _, _ = make_service()
    await service.register("alice", org_credential())
    await service.activate("alice", TrustLevel.LURKER, "fleet-a", admin_credential())
    with pytest.raises(ValueError):
        await service.register("alice", org_credential())


async def test_register_with_agent_key_denied() -> None:
    service, _, _ = make_service()
    with pytest.raises(PermissionDenied):
        await service.register("alice", agent_credential())


async def test_register_org_only_allows_org_key() -> None:
    # The MCP hive_register gate (SPEC §5.2): org key only.
    service, _, _ = make_service()
    agent = await service.register("alice", org_credential(), org_only=True)
    assert agent.status is AgentStatus.PENDING


async def test_register_org_only_denies_admin_key() -> None:
    # org_only=True rejects the admin key (the REST surface accepts it;
    # the MCP verb does not — SPEC §5.1 vs §5.2).
    service, _, _ = make_service()
    with pytest.raises(PermissionDenied):
        await service.register("alice", admin_credential(), org_only=True)


# -- activation (admin-gated; key issued once, ADR 0012) -------------------


async def test_activate_with_admin_issues_key_once() -> None:
    service, auth, store = make_service()
    await service.register("alice", org_credential())
    await store.create_fleet("data-eng")  # ensure a fleet exists for the FK
    agent, key = await service.activate(
        "alice", TrustLevel.CONTRIBUTOR, (await store.list_fleets())[0].id, admin_credential()
    )
    assert agent.status is AgentStatus.ACTIVE
    assert agent.trust_level is TrustLevel.CONTRIBUTOR
    assert key  # the raw key is returned
    # The key is verified (resolves back to the agent's credential).
    assert auth._by_key.get(key) is not None


async def test_activate_with_non_admin_denied() -> None:
    service, _, _ = make_service()
    with pytest.raises(PermissionDenied):
        await service.activate("alice", TrustLevel.LURKER, "f", org_credential())


# -- fleet / level / fleet-move (admin-gated, ADR 0011) --------------------


async def test_create_fleet_admin_only() -> None:
    service, _, _ = make_service()
    fleet = await service.create_fleet("data-eng", admin_credential())
    assert fleet.name == "data-eng"
    with pytest.raises(PermissionDenied):
        await service.create_fleet("ml", org_credential())


async def test_set_trust_level_demotes_dormant() -> None:
    service, _, _ = make_service()
    await service.register("alice", org_credential())
    fleet = await service.create_fleet("data-eng", admin_credential())
    await service.activate("alice", TrustLevel.PRIVILEGED, fleet.id, admin_credential())
    demoted = await service.set_trust_level("alice", TrustLevel.UNTRUSTED, admin_credential())
    # Demotion to level 0 is *dormant* (still active, no access) — not
    # revocation (ADR 0012: demotion != revocation).
    assert demoted.trust_level is TrustLevel.UNTRUSTED
    assert demoted.status is AgentStatus.ACTIVE


async def test_set_home_fleet_moves_agent() -> None:
    service, _, _ = make_service()
    await service.register("alice", org_credential())
    fa = await service.create_fleet("data-eng", admin_credential())
    fb = await service.create_fleet("ml", admin_credential())
    await service.activate("alice", TrustLevel.LURKER, fa.id, admin_credential())
    moved = await service.set_home_fleet("alice", fb.id, admin_credential())
    assert moved.home_fleet_id == fb.id


# -- revocation + org-key rotation (admin-gated, ADR 0012) -----------------


async def test_revoke_admin_only() -> None:
    service, _, _ = make_service()
    with pytest.raises(PermissionDenied):
        await service.revoke("alice", org_credential())
    # Admin revocation succeeds (no error).
    await service.register("alice", org_credential())
    await service.revoke("alice", admin_credential())  # should not raise


async def test_rotate_org_key_admin_only() -> None:
    service, _, _ = make_service()
    with pytest.raises(PermissionDenied):
        await service.rotate_org_key(org_credential())
    new_key = await service.rotate_org_key(admin_credential())
    assert new_key  # a new raw key is returned once


# -- write-scope resolution (ADR 0011) --------------------------------------


def test_resolve_default_scope_by_level() -> None:
    # L1 (lurker): default is 'self'.
    assert resolve_write_scope(agent_credential(TrustLevel.LURKER)).scope == "self"
    # L2/L3: default is 'fleet'.
    assert resolve_write_scope(agent_credential(TrustLevel.CONTRIBUTOR)).scope == "fleet"
    assert resolve_write_scope(agent_credential(TrustLevel.PRIVILEGED)).scope == "fleet"


def test_resolve_lurker_cannot_write_fleet() -> None:
    with pytest.raises(PermissionDenied):
        resolve_write_scope(agent_credential(TrustLevel.LURKER), requested_scope="fleet")


def test_resolve_l3_can_write_self_or_fleet() -> None:
    res = resolve_write_scope(agent_credential(TrustLevel.PRIVILEGED), requested_scope="self")
    assert res.scope == "self" and res.fleet_id is None
    res = resolve_write_scope(agent_credential(TrustLevel.PRIVILEGED), requested_scope="fleet")
    assert res.scope == "fleet" and res.fleet_id == "fleet-a"


def test_resolve_l3_cannot_write_org() -> None:
    # 'org' is a legacy read-only scope; agents write self/fleet only (ADR 0011).
    with pytest.raises(PermissionDenied):
        resolve_write_scope(agent_credential(TrustLevel.PRIVILEGED), requested_scope="org")


def test_resolve_untrusted_cannot_write() -> None:
    with pytest.raises(PermissionDenied):
        resolve_write_scope(agent_credential(TrustLevel.UNTRUSTED))


def test_resolve_fleet_scope_requires_home_fleet() -> None:
    # An L2 agent with no home fleet cannot write 'fleet' scope.
    with pytest.raises(PermissionDenied):
        resolve_write_scope(
            agent_credential(TrustLevel.CONTRIBUTOR, home_fleet=None), requested_scope="fleet"
        )


def test_resolve_legacy_writes_any_scope() -> None:
    # Legacy (v1) credentials keep the flat-pool behavior: any scope, org default.
    assert resolve_write_scope(legacy_credential()).scope == "org"
    assert resolve_write_scope(legacy_credential(), requested_scope="self").scope == "self"
    assert resolve_write_scope(legacy_credential(), requested_scope="fleet").scope == "fleet"