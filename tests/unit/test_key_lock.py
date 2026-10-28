"""Locking an agent's own key from the portal (ADR 0117, SPEC §12.3).

Only the key is locked: the agent stays active with everything it had. The
locked key fails every surface with ``key_locked``; an admin's key replacement
clears the lock; the audit row holds the fingerprint, never a key.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from hivemind.api.deps import HivemindApp, create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, KeyLocked, TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters
from hivemind.mcp.http import BearerAuthMiddleware, current_credential
from hivemind.mcp.server import build_server
from hivemind.ports import Credential
from hivemind.services.access import AccessService, PermissionDenied
from tests.fakes import (
    FakeAuthenticator,
    fake_key_id,
    make_embedder,
    make_search_config,
    make_store,
)
from tests.unit.test_mcp_http import make_app

ADMIN = Credential(user_id="admin", is_admin=True, key_id="adminfinger1")
ORG = Credential(user_id="org", is_org=True, access_controlled=True)
ADMIN_H = {"X-API-Key": "hm_admin"}


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


async def _active_agent(app: HivemindApp, name: str = "alice") -> str:
    fleet = await app.store.create_fleet(f"fleet-{name}")
    await app.store.register_agent(name)
    _, key = await app.access_service.activate(name, TrustLevel.CONTRIBUTOR, fleet.id, ADMIN)
    return key


async def test_a_locked_key_fails_the_next_request_with_key_locked() -> None:
    app = _app()
    key = await _active_agent(app)
    headers = {"X-API-Key": key}
    async with _client(app) as client:
        assert (await client.get("/v1/whoami", headers=headers)).status_code == 200
        locked = await client.post("/v1/portal/key/lock", headers=headers)
        after = await client.get("/v1/whoami", headers=headers)
        again = await client.post("/v1/portal/key/lock", headers=headers)
    assert locked.status_code == 200
    assert locked.json()["locked"] is True and locked.json()["key_id"] == fake_key_id(key)
    assert after.status_code == 401
    error = after.json()["error"]
    assert error["code"] == "key_locked"
    assert "ask the admin for a new key" in error["message"]
    assert again.status_code == 401 and again.json()["error"]["code"] == "key_locked"


async def test_only_the_key_is_locked_the_agent_is_untouched() -> None:
    app = _app()
    key = await _active_agent(app)
    before = await app.store.get_agent("alice")
    async with _client(app) as client:
        await client.post("/v1/portal/key/lock", headers={"X-API-Key": key})
        listed = await client.get("/v1/admin/agents", headers=ADMIN_H)
    after = await app.store.get_agent("alice")
    assert before is not None and after is not None
    assert after.status is AgentStatus.ACTIVE
    assert (after.trust_level, after.home_fleet_id) == (before.trust_level, before.home_fleet_id)
    [row] = listed.json()
    assert row["status"] == "active" and row["key_locked"] is True and row["key_locked_at"]


async def test_the_admin_list_says_which_keys_are_locked_and_keeps_idle_working() -> None:
    app = _app()
    key = await _active_agent(app, "alice")
    await _active_agent(app, "bob")
    async with _client(app) as client:
        await client.post("/v1/portal/key/lock", headers={"X-API-Key": key})
        rows = {
            r["name"]: r for r in (await client.get("/v1/admin/agents", headers=ADMIN_H)).json()
        }
    assert rows["alice"]["key_locked"] is True and rows["bob"]["key_locked"] is False
    assert rows["bob"]["key_locked_at"] is None
    assert {"idle", "idle_after_days", "last_used_at"} <= rows["alice"].keys()


async def test_admin_and_org_keys_get_403_on_the_lock_route() -> None:
    app = _app()
    async with _client(app) as client:
        admin = await client.post("/v1/portal/key/lock", headers=ADMIN_H)
        org = await client.post("/v1/portal/key/lock", headers={"X-API-Key": "hm_org"})
        anonymous = await client.post("/v1/portal/key/lock")
    assert (admin.status_code, org.status_code, anonymous.status_code) == (403, 403, 401)
    assert admin.json()["error"]["code"] == "forbidden"


async def test_replace_key_clears_the_lock_and_works_on_a_locked_active_agent() -> None:
    app = _app()
    key = await _active_agent(app)
    async with _client(app) as client:
        await client.post("/v1/portal/key/lock", headers={"X-API-Key": key})
        replaced = await client.post("/v1/admin/agents/alice/key", headers=ADMIN_H)
        old = await client.get("/v1/whoami", headers={"X-API-Key": key})
        new = await client.get("/v1/whoami", headers={"X-API-Key": replaced.json()["key"]})
        listed = await client.get("/v1/admin/agents", headers=ADMIN_H)
    assert replaced.status_code == 200
    assert old.status_code == 401
    assert new.status_code == 200 and new.json()["name"] == "alice"
    assert listed.json()[0]["key_locked"] is False


async def test_revoking_a_locked_agent_still_works_and_its_key_is_just_unknown() -> None:
    app = _app()
    key = await _active_agent(app)
    async with _client(app) as client:
        await client.post("/v1/portal/key/lock", headers={"X-API-Key": key})
        revoked = await client.post("/v1/admin/agents/alice/revoke", headers=ADMIN_H)
        after = await client.get("/v1/whoami", headers={"X-API-Key": key})
    assert revoked.status_code == 200
    assert after.status_code == 401 and after.json()["error"]["code"] == "unknown_api_key"


async def test_the_audit_row_is_an_agent_key_row_with_the_fingerprint_and_no_key() -> None:
    app = _app()
    key = await _active_agent(app)
    async with _client(app) as client:
        await client.post("/v1/portal/key/lock", headers={"X-API-Key": key})
        await client.post("/v1/portal/key/lock", headers={"X-API-Key": key})  # refused: locked
    rows = await app.access_service.list_audit(
        ADMIN, AuditFilters(action=AuditAction.AGENT_KEY_LOCK), 10
    )
    [row] = rows  # a second press audits nothing
    assert (row.actor_kind, row.actor, row.target) == (ActorKind.AGENT_KEY, "alice", "alice")
    assert dict(row.detail) == {"key": fake_key_id(key), "via": "portal"}
    assert key not in repr(row)


async def test_the_service_refuses_anything_but_an_agent_key() -> None:
    store = make_store()
    service = AccessService(store, FakeAuthenticator(store=store))
    dev = Credential(user_id="dev")
    for caller in (ADMIN, ORG, dev):
        with pytest.raises(PermissionDenied):
            await service.lock_own_key(caller)


async def test_verify_raises_key_locked_only_for_an_active_agent() -> None:
    store = make_store()
    auth = FakeAuthenticator(store=store)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", ADMIN)
    await service.register("alice", ORG, owner_alias="x")
    _, key = await service.activate("alice", TrustLevel.LURKER, fleet.id, ADMIN)
    credential = await auth.verify(key)
    assert credential is not None
    await service.lock_own_key(credential)
    with pytest.raises(KeyLocked):
        await auth.verify(key)
    await service.revoke("alice", ADMIN)
    assert await auth.verify(key) is None  # revoked: unknown again, not "locked"


# -- the other surfaces ------------------------------------------------------------


async def test_mcp_http_answers_401_key_locked_with_what_to_do() -> None:
    auth = FakeAuthenticator(
        {"key-alice": Credential(user_id="alice", agent_name="alice", access_controlled=True)}
    )
    auth._locked.add("key-alice")
    called: list[bool] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        called.append(True)

    sent: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        sent.append(msg)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "headers": [(b"authorization", b"Bearer key-alice")]}
    await BearerAuthMiddleware(inner, auth)(scope, receive, send)
    assert not called and current_credential() is None
    assert sent[0]["status"] == 401
    assert (b"www-authenticate", b'Bearer realm="hivemind"') in sent[0]["headers"]
    body = json.loads(sent[1]["body"])
    assert body["error"] == "key_locked" and "ask the admin for a new key" in body["detail"]


async def test_a_mcp_tool_call_with_a_locked_key_returns_key_locked() -> None:
    alice = Credential(user_id="alice", agent_name="alice", access_controlled=True)
    auth = FakeAuthenticator({"key-alice": alice})

    async def reverify() -> Credential | None:
        return await auth.verify("key-alice")

    server = build_server(make_app(alice), credential_provider=reverify)
    ok = await server.call_tool("hive_whoami", {})
    assert "error" not in json.loads(ok.content[0].text)
    auth._locked.add("key-alice")
    locked = await server.call_tool("hive_whoami", {})
    error = json.loads(locked.content[0].text)["error"]
    assert error["code"] == "key_locked" and "ask the admin" in error["message"]


async def test_a_press_for_a_key_replaced_meanwhile_answers_409_and_changes_nothing() -> None:
    class RacedAuth(FakeAuthenticator):
        async def lock_agent_key(self, agent_name: str, *, key_id: str) -> bool:
            return False  # the key was replaced between the check and the lock

    store = make_store()
    app = create_app_for_config(
        Settings(),
        store=store,
        embedder=make_embedder(),
        authenticator=RacedAuth(store=store),
        search_config=make_search_config(),
    )
    key = await _active_agent(app)
    async with _client(app) as client:
        pressed = await client.post("/v1/portal/key/lock", headers={"X-API-Key": key})
        still = await client.get("/v1/whoami", headers={"X-API-Key": key})
    assert pressed.status_code == 409
    assert pressed.json()["error"]["code"] == "key_not_locked"
    assert still.status_code == 200


def test_the_stdio_runner_exits_with_the_lock_message_on_a_locked_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from hivemind.domain.access import KEY_LOCKED_MESSAGE
    from hivemind.mcp import runners

    store = make_store()
    auth = FakeAuthenticator(store=store)

    async def setup() -> str:
        service = AccessService(store, auth)
        fleet = await service.create_fleet("f", ADMIN)
        await service.register("alice", ORG, owner_alias="x")
        _, key = await service.activate("alice", TrustLevel.LURKER, fleet.id, ADMIN)
        credential = await auth.verify(key)
        assert credential is not None
        await service.lock_own_key(credential)
        return key

    key = asyncio.run(setup())
    monkeypatch.setenv("HIVEMIND_MCP_KEY", key)
    monkeypatch.setattr("hivemind.store.build_store", lambda settings: store)
    monkeypatch.setattr("hivemind.store.build_authenticator", lambda settings: auth)
    monkeypatch.setattr("hivemind.embeddings.build_embedder", lambda settings: make_embedder())
    with pytest.raises(SystemExit) as exit_info:
        runners.main_pg()
    assert str(exit_info.value) == KEY_LOCKED_MESSAGE
