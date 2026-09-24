"""The admin-surface audit log, path 1 (ADR 0027, SPEC §12.5): every
``AccessService`` mutation and an admin's withdrawal of someone else's
entry each write exactly one ``admin_key`` row; an audit-write failure
is raised, never swallowed; no raw key reaches an audit row; and the
``GET /v1/admin/audit-log`` read surface (auth gate + filters).

Seams: the services over ``MemoryStore`` + ``FakeAuthenticator``, and
the REST app over the same fakes.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import httpx
import pytest

from hivemind.api.deps import HivemindApp, create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain import (
    ActorKind,
    AuditAction,
    AuditEvent,
    AuditFilters,
    AuditRecord,
    EntryDraft,
    Kind,
    TrustLevel,
)
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.services.governance import GovernanceService, PermissionDenied
from tests.fakes import (
    FakeAuthenticator,
    fake_key_id,
    make_clock,
    make_embedder,
    make_search_config,
)

ADMIN_KEY = "hm_admin"
ADMIN_ACTOR = f"admin:{fake_key_id(ADMIN_KEY)}"


class FailingAuditStore(MemoryStore):
    """A ``MemoryStore`` whose audit write fails (the store is down, a
    CHECK rejects the row, ...)."""

    async def record_audit(self, event: AuditEvent) -> AuditRecord:
        raise RuntimeError("audit_log is unwritable")


async def _admin(auth: FakeAuthenticator) -> Credential:
    credential = await auth.verify(ADMIN_KEY)
    assert credential is not None and credential.is_admin
    return credential


async def _setup(
    store: MemoryStore | None = None,
) -> tuple[AccessService, MemoryStore, FakeAuthenticator, Credential, str]:
    """An AccessService with one pending agent (alice) and one fleet."""
    store = store or MemoryStore(make_clock())
    auth = FakeAuthenticator()
    await store.register_agent("alice")
    fleet = await store.create_fleet("data-eng")
    return AccessService(store, auth), store, auth, await _admin(auth), fleet.id


async def _rows(store: MemoryStore) -> list[AuditRecord]:
    return await store.list_audit(AuditFilters(), limit=1000)


# -- one row per AccessService action ------------------------------------------


async def test_activate_writes_one_row() -> None:
    service, store, _, admin, fleet_id = await _setup()
    await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet_id, admin)
    [row] = await _rows(store)
    assert row.actor_kind is ActorKind.ADMIN_KEY
    assert row.actor == ADMIN_ACTOR
    assert row.action is AuditAction.AGENT_ACTIVATE
    assert row.target == "alice"
    assert row.detail == {"trust_level": 2, "home_fleet_id": fleet_id}


async def test_create_fleet_writes_one_row() -> None:
    service, store, _, admin, _ = await _setup()
    fleet = await service.create_fleet("ml", admin)
    [row] = await _rows(store)
    assert row.action is AuditAction.FLEET_CREATE
    assert row.target == fleet.id
    assert row.detail == {"name": "ml"}
    assert row.actor == ADMIN_ACTOR


async def test_set_trust_level_records_from_and_to() -> None:
    service, store, _, admin, fleet_id = await _setup()
    await store.activate_agent("alice", trust_level=TrustLevel.LURKER, home_fleet_id=fleet_id)
    await service.set_trust_level("alice", TrustLevel.PRIVILEGED, admin)
    [row] = await _rows(store)
    assert row.action is AuditAction.AGENT_TRUST_LEVEL_SET
    assert row.target == "alice"
    assert row.detail == {"from": 1, "to": 3}


async def test_set_home_fleet_records_from_and_to() -> None:
    service, store, _, admin, fleet_id = await _setup()
    await store.activate_agent("alice", trust_level=TrustLevel.LURKER, home_fleet_id=fleet_id)
    other = await store.create_fleet("ml")
    await service.set_home_fleet("alice", other.id, admin)
    [row] = await _rows(store)
    assert row.action is AuditAction.AGENT_HOME_FLEET_SET
    assert row.target == "alice"
    assert row.detail == {"from": fleet_id, "to": other.id}


async def test_revoke_writes_one_row() -> None:
    service, store, _, admin, _ = await _setup()
    await service.revoke("alice", admin)
    [row] = await _rows(store)
    assert row.action is AuditAction.AGENT_REVOKE
    assert row.target == "alice"
    assert row.detail == {"from": "pending"}


async def test_rotate_org_key_writes_one_row_with_no_target() -> None:
    service, store, _, admin, _ = await _setup()
    await service.rotate_org_key(admin)
    [row] = await _rows(store)
    assert row.action is AuditAction.ORG_KEY_ROTATE
    assert row.target is None
    assert row.detail == {}


async def test_a_denied_action_writes_nothing() -> None:
    service, store, _, _, _ = await _setup()
    org = Credential(user_id="org", is_org=True, access_controlled=True)
    with pytest.raises(PermissionDenied):
        await service.create_fleet("ml", org)
    assert await _rows(store) == []


async def test_a_failed_mutation_writes_nothing() -> None:
    service, store, _, admin, _ = await _setup()
    with pytest.raises(KeyError):
        await service.set_trust_level("nobody", TrustLevel.LURKER, admin)
    assert await _rows(store) == []


async def test_register_and_reads_are_not_audited() -> None:
    """Deliberately out of scope (ADR 0027): self-service registration
    grants no privilege (activation does, and is audited), and the
    listings are read-only."""
    service, store, _, admin, _ = await _setup()
    org = Credential(user_id="org", is_org=True, access_controlled=True)
    await service.register("bob", org)
    await service.list_agents(admin)
    await service.list_fleets(admin)
    assert await _rows(store) == []


async def test_actor_falls_back_to_user_id_without_a_fingerprint() -> None:
    service, store, _, _, _ = await _setup()
    await service.create_fleet("ml", Credential(user_id="ops", is_admin=True))
    [row] = await _rows(store)
    assert row.actor == "ops"


# -- the audit write fails loudly ---------------------------------------------


async def test_audit_write_failure_raises_after_the_mutation() -> None:
    """Not atomic (two ports, no shared transaction — ADR 0027): the
    mutation has happened, and the caller must see it went unaudited."""
    service, store, _, admin, _ = await _setup(FailingAuditStore(make_clock()))
    with pytest.raises(RuntimeError, match="audit_log is unwritable"):
        await service.create_fleet("ml", admin)
    assert "ml" in {f.name for f in await store.list_fleets()}


async def test_withdraw_audit_failure_raises() -> None:
    store = FailingAuditStore(make_clock())
    entry = await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="x", author="alice", agent="a1")
    )
    with pytest.raises(RuntimeError):
        await GovernanceService(store).withdraw(
            Credential(user_id="admin", is_admin=True, key_id="abc"), entry.id, "bad"
        )


# -- entry.withdraw: only when admin privilege authorised it ------------------


async def test_admin_withdrawing_anothers_entry_writes_one_row() -> None:
    store = MemoryStore(make_clock())
    entry = await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="x", author="alice", agent="a1")
    )
    admin = Credential(user_id="admin", is_admin=True, key_id="0123456789ab")
    await GovernanceService(store).withdraw(admin, entry.id, "wrong fleet")
    [row] = await _rows(store)
    assert row.action is AuditAction.ENTRY_WITHDRAW
    assert row.actor == "admin:0123456789ab"
    assert row.target == entry.id
    assert row.detail == {"author": "alice", "reason": "wrong fleet"}


async def test_author_withdrawing_own_entry_writes_nothing() -> None:
    store = MemoryStore(make_clock())
    entry = await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="x", author="alice", agent="a1")
    )
    await GovernanceService(store).withdraw(
        Credential(user_id="alice", agent_id="a1"), entry.id, "stale"
    )
    assert await _rows(store) == []


async def test_admin_withdrawing_own_entry_writes_nothing() -> None:
    """Authorship alone authorised it — no admin privilege was used."""
    store = MemoryStore(make_clock())
    entry = await store.create_entry(
        EntryDraft(kind=Kind.FACT, summary="x", author="admin", agent="a1")
    )
    await GovernanceService(store).withdraw(
        Credential(user_id="admin", is_admin=True, key_id="abc"), entry.id, None
    )
    assert await _rows(store) == []


# -- no raw key in any audit row ----------------------------------------------


async def test_no_raw_key_appears_in_any_audit_row() -> None:
    """Every key-producing app action (activate, rotate_org_key), and the
    rest for good measure: no raw key string anywhere in any column."""
    service, store, _, admin, fleet_id = await _setup()
    raw_keys = [
        (await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet_id, admin))[1],
        await service.rotate_org_key(admin),
        await service.rotate_org_key(admin),
        ADMIN_KEY,
    ]
    await service.set_trust_level("alice", TrustLevel.PRIVILEGED, admin)
    await service.revoke("alice", admin)
    rows = await _rows(store)
    assert len(rows) == 5
    serialized = json.dumps([asdict(r) for r in rows], default=str)
    for raw in raw_keys:
        assert raw not in serialized


# -- GET /v1/admin/audit-log ----------------------------------------------------


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


async def test_endpoint_requires_an_admin_key() -> None:
    async with _client(_app()) as client:
        missing = await client.get("/v1/admin/audit-log")
        org = await client.get("/v1/admin/audit-log", headers={"X-API-Key": "hm_org"})
    assert missing.status_code == 401
    assert org.status_code == 403
    assert org.json()["error"]["code"] == "forbidden"


async def test_endpoint_lists_newest_first_with_filters() -> None:
    app = _app()
    headers = {"X-API-Key": ADMIN_KEY}
    await app.store.register_agent("alice")
    async with _client(app) as client:
        fleet = (
            await client.post("/v1/admin/fleets", json={"name": "data-eng"}, headers=headers)
        ).json()
        await client.post(
            "/v1/admin/agents/alice/activate",
            json={"trust_level": 1, "home_fleet_id": fleet["id"]},
            headers=headers,
        )
        await client.post("/v1/admin/agents/alice/revoke", headers=headers)
        # A row by another actor, to filter out.
        await app.store.record_audit(
            AuditEvent(ActorKind.CLI, "chris", AuditAction.ADMIN_KEY_ISSUE, "0123456789ab")
        )

        everything = await client.get("/v1/admin/audit-log", headers=headers)
        by_actor = await client.get(
            "/v1/admin/audit-log", params={"actor": ADMIN_ACTOR}, headers=headers
        )
        by_action = await client.get(
            "/v1/admin/audit-log", params={"action": "agent.revoke"}, headers=headers
        )
        limited = await client.get("/v1/admin/audit-log", params={"limit": 1}, headers=headers)
        future = await client.get(
            "/v1/admin/audit-log", params={"since": "2999-01-01T00:00:00"}, headers=headers
        )
        bad_action = await client.get(
            "/v1/admin/audit-log", params={"action": "entry.create"}, headers=headers
        )
        bad_limit = await client.get(
            "/v1/admin/audit-log", params={"limit": 100000}, headers=headers
        )

    assert everything.status_code == 200
    assert [r["action"] for r in everything.json()] == [
        "admin_key.issue",
        "agent.revoke",
        "agent.activate",
        "fleet.create",
    ]
    assert [r["action"] for r in by_actor.json()] == [
        "agent.revoke",
        "agent.activate",
        "fleet.create",
    ]
    assert {r["actor_kind"] for r in by_actor.json()} == {"admin_key"}
    [revoke] = by_action.json()
    assert revoke["target"] == "alice" and revoke["actor"] == ADMIN_ACTOR
    assert [r["action"] for r in limited.json()] == ["admin_key.issue"]
    assert future.json() == []
    assert bad_action.status_code == 422
    assert bad_limit.status_code == 422
