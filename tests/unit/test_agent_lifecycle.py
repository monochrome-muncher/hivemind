"""The agent lifecycle and its guards (ADR 0028, SPEC §12.3).

pending → active (activate), pending → revoked (reject), active → revoked
(revoke), revoked → active (re-activate, fresh key). Every other
transition is refused, so an agent never holds two keys at once.
"""

from __future__ import annotations

import httpx
import pytest

from hivemind.api.deps import HivemindApp, create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, InvalidAgentStatus, TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent, AuditFilters
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from tests.fakes import (
    FakeAuthenticator,
    make_clock,
    make_embedder,
    make_search_config,
    make_store,
)

ADMIN = Credential(user_id="admin", is_admin=True)
ORG = Credential(user_id="org", is_org=True, access_controlled=True)


async def _service() -> tuple[AccessService, FakeAuthenticator, str]:
    store = make_store()
    auth = FakeAuthenticator()
    service = AccessService(store, auth)
    fleet = await service.create_fleet("data-eng", ADMIN)
    await service.register("alice", ORG, owner_alias="chris")
    return service, auth, fleet.id


async def test_activate_an_active_agent_is_refused_and_issues_no_second_key() -> None:
    service, auth, fleet = await _service()
    _, first = await service.activate("alice", TrustLevel.LURKER, fleet, ADMIN)
    with pytest.raises(InvalidAgentStatus):
        await service.activate("alice", TrustLevel.PRIVILEGED, fleet, ADMIN)
    # The refused call issued nothing: the one key is still the live one.
    credential = await auth.verify(first)
    assert credential is not None and credential.agent_name == "alice"


async def test_revoke_from_pending_rejects_the_registration() -> None:
    service, _, _ = await _service()
    await service.revoke("alice", ADMIN)
    agents = {a.name: a for a in await service.list_agents(ADMIN)}
    assert agents["alice"].status is AgentStatus.REVOKED
    # The name stays reserved: re-registering it is a conflict.
    with pytest.raises(ValueError, match="revoked"):
        await service.register("alice", ORG)


async def test_revoke_kills_the_key_and_reactivation_issues_a_fresh_one() -> None:
    service, auth, fleet = await _service()
    _, old = await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet, ADMIN)
    await service.revoke("alice", ADMIN)
    assert await auth.verify(old) is None
    agent, new = await service.activate("alice", TrustLevel.LURKER, fleet, ADMIN)
    assert agent.status is AgentStatus.ACTIVE
    # (The fake derives keys from the name; key freshness is pinned
    # against the real authenticator in tests/integration.)
    assert await auth.verify(new) is not None


async def test_revoke_twice_and_revoke_unknown_are_refused() -> None:
    service, _, _ = await _service()
    await service.revoke("alice", ADMIN)
    with pytest.raises(InvalidAgentStatus):
        await service.revoke("alice", ADMIN)
    with pytest.raises(KeyError):
        await service.revoke("nobody", ADMIN)


async def test_revoke_records_the_status_it_came_from() -> None:
    service, _, fleet = await _service()
    await service.register("bob", ORG)
    await service.revoke("bob", ADMIN)  # a rejection
    await service.activate("alice", TrustLevel.LURKER, fleet, ADMIN)
    await service.revoke("alice", ADMIN)
    rows = await service.list_audit(ADMIN, AuditFilters(action=AuditAction.AGENT_REVOKE), 10)
    assert [(r.target, dict(r.detail)) for r in rows] == [
        ("alice", {"from": "active"}),
        ("bob", {"from": "pending"}),
    ]


# -- over HTTP: status codes -------------------------------------------------


def _app() -> HivemindApp:
    return create_app_for_config(
        Settings(),
        store=MemoryStore(make_clock()),
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(),
        search_config=make_search_config(),
    )


def _client(app: HivemindApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://hivemind.test"
    )


async def test_http_status_codes_for_refused_transitions() -> None:
    app = _app()
    headers = {"X-API-Key": "hm_admin"}
    await app.store.register_agent("alice")
    fleet = await app.store.create_fleet("data-eng")
    body = {"trust_level": 1, "home_fleet_id": fleet.id}
    async with _client(app) as client:
        ok = await client.post("/v1/admin/agents/alice/activate", json=body, headers=headers)
        again = await client.post("/v1/admin/agents/alice/activate", json=body, headers=headers)
        revoked = await client.post("/v1/admin/agents/alice/revoke", headers=headers)
        twice = await client.post("/v1/admin/agents/alice/revoke", headers=headers)
        unknown = await client.post("/v1/admin/agents/nobody/revoke", headers=headers)
        listing = await client.get("/v1/admin/agents", headers=headers)
        metrics = await client.get("/v1/metrics", headers=headers)
    assert ok.status_code == 200
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "invalid_status"
    assert revoked.status_code == 200
    assert twice.status_code == 409
    assert unknown.status_code == 404
    assert [a["status"] for a in listing.json()] == ["revoked"]
    assert metrics.json()["agents"]["revoked"] == 1


async def test_http_audit_log_pages_back_with_before() -> None:
    app = _app()
    headers = {"X-API-Key": "hm_admin"}
    for n in range(5):
        await app.store.record_audit(
            AuditEvent(ActorKind.CLI, "chris", AuditAction.FLEET_CREATE, f"f{n}")
        )
    async with _client(app) as client:
        first = (
            await client.get("/v1/admin/audit-log", params={"limit": 2}, headers=headers)
        ).json()
        second = (
            await client.get(
                "/v1/admin/audit-log",
                params={"limit": 2, "before": first[-1]["id"]},
                headers=headers,
            )
        ).json()
        rest = (
            await client.get(
                "/v1/admin/audit-log",
                params={"limit": 10, "before": second[-1]["id"]},
                headers=headers,
            )
        ).json()
        unknown = await client.get(
            "/v1/admin/audit-log",
            params={"before": "00000000-0000-0000-0000-000000000000"},
            headers=headers,
        )
        malformed = await client.get(
            "/v1/admin/audit-log", params={"before": "nope"}, headers=headers
        )
    assert [r["target"] for r in first + second + rest] == ["f4", "f3", "f2", "f1", "f0"]
    assert unknown.json() == []
    assert malformed.status_code == 422
