"""Replacing an active agent's key (ADR 0108, SPEC §12.3).

Admin-only; the old key dies at once; only an ``active`` agent qualifies;
the agent record is untouched; the audit row holds fingerprints, never a key.
"""

from __future__ import annotations

import httpx
import pytest

from hivemind.api.deps import HivemindApp, create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, InvalidAgentStatus, TrustLevel, UnknownAgent
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.ports import Credential
from hivemind.services.access import AccessService, PermissionDenied
from tests.fakes import (
    FakeAuthenticator,
    fake_key_id,
    make_embedder,
    make_search_config,
    make_store,
)

ADMIN = Credential(user_id="admin", is_admin=True, key_id="adminfinger1")
ORG = Credential(user_id="org", is_org=True, access_controlled=True)
AGENT = Credential(user_id="alice", agent_name="alice", access_controlled=True)


async def _service() -> tuple[AccessService, FakeAuthenticator, str, object]:
    store = make_store()
    auth = FakeAuthenticator(store=store)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("data-eng", ADMIN)
    await service.register("alice", ORG, owner_alias="john")
    return service, auth, fleet.id, store


async def test_replace_kills_the_old_key_at_once_and_returns_a_working_new_one() -> None:
    service, auth, fleet, _ = await _service()
    _, old = await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet, ADMIN)
    new = await service.replace_agent_key("alice", ADMIN)
    assert new != old
    assert await auth.verify(old) is None
    credential = await auth.verify(new)
    assert credential is not None and credential.agent_name == "alice"


async def test_replace_leaves_the_agent_record_alone() -> None:
    service, _, fleet, store = await _service()
    await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet, ADMIN)
    before = await store.get_agent("alice")
    await service.replace_agent_key("alice", ADMIN)
    assert await store.get_agent("alice") == before


@pytest.mark.parametrize("caller", [ORG, AGENT], ids=["org key", "agent key"])
async def test_only_an_admin_may_replace_a_key(caller: Credential) -> None:
    service, auth, fleet, _ = await _service()
    _, old = await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet, ADMIN)
    with pytest.raises(PermissionDenied):
        await service.replace_agent_key("alice", caller)
    assert await auth.verify(old) is not None  # nothing happened


@pytest.mark.parametrize("status", ["pending", "revoked"])
async def test_a_pending_or_revoked_agent_is_refused(status: str) -> None:
    service, _, fleet, store = await _service()
    if status == "revoked":
        await service.activate("alice", TrustLevel.LURKER, fleet, ADMIN)
        await service.revoke("alice", ADMIN)
    with pytest.raises(InvalidAgentStatus):
        await service.replace_agent_key("alice", ADMIN)
    agent = await store.get_agent("alice")
    assert agent is not None and agent.status is AgentStatus(status)
    rows = await service.list_audit(ADMIN, AuditFilters(action=AuditAction.AGENT_KEY_REPLACE), 10)
    assert rows == []


async def test_an_unknown_agent_is_refused() -> None:
    service, _, _, _ = await _service()
    with pytest.raises(UnknownAgent):
        await service.replace_agent_key("nobody", ADMIN)


async def test_the_audit_row_has_both_fingerprints_and_no_key() -> None:
    service, _, fleet, _ = await _service()
    _, old = await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet, ADMIN)
    new = await service.replace_agent_key("alice", ADMIN)
    [row] = await service.list_audit(ADMIN, AuditFilters(action=AuditAction.AGENT_KEY_REPLACE), 10)
    assert row.target == "alice"
    assert row.actor == "admin:adminfinger1"
    assert dict(row.detail) == {"old_key": fake_key_id(old), "new_key": fake_key_id(new)}
    assert old not in repr(row) and new not in repr(row)


# -- over HTTP ---------------------------------------------------------------


def _app() -> HivemindApp:
    store = make_store()
    return create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(store=store),
        search_config=make_search_config(),
    )


def _client(app: HivemindApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://hivemind.test"
    )


async def test_http_replace_status_codes_and_the_key_in_the_body() -> None:
    app = _app()
    admin = {"X-API-Key": "hm_admin"}
    await app.store.register_agent("alice")
    fleet = await app.store.create_fleet("data-eng")
    body = {"trust_level": 1, "home_fleet_id": fleet.id}
    async with _client(app) as client:
        pending = await client.post("/v1/admin/agents/alice/key", headers=admin)
        issued = await client.post("/v1/admin/agents/alice/activate", json=body, headers=admin)
        org = await client.post("/v1/admin/agents/alice/key", headers={"X-API-Key": "hm_org"})
        no_key = await client.post("/v1/admin/agents/alice/key")
        agent_key = {"X-API-Key": issued.json()["key"]}
        agent_self = await client.post("/v1/admin/agents/alice/key", headers=agent_key)
        # A non-admin is refused before it learns whether the agent exists.
        org_nobody = await client.post(
            "/v1/admin/agents/nobody/key", headers={"X-API-Key": "hm_org"}
        )
        agent_nobody = await client.post("/v1/admin/agents/nobody/key", headers=agent_key)
        unknown = await client.post("/v1/admin/agents/nobody/key", headers=admin)
        replaced = await client.post("/v1/admin/agents/alice/key", headers=admin)
        old_works = await client.get("/v1/whoami", headers={"X-API-Key": issued.json()["key"]})
        new_works = await client.get("/v1/whoami", headers={"X-API-Key": replaced.json()["key"]})
    assert pending.status_code == 409 and pending.json()["error"]["code"] == "invalid_status"
    assert org.status_code == 403
    assert agent_self.status_code == org_nobody.status_code == agent_nobody.status_code == 403
    assert no_key.status_code == 401
    assert unknown.status_code == 404
    assert replaced.status_code == 200
    assert replaced.json()["key"] != issued.json()["key"]
    assert old_works.status_code == 401
    assert new_works.status_code == 200
