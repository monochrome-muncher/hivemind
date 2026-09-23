"""The audit log at the ``Store`` seam (ADR 0027, SPEC §12.5), on the
in-memory reference store, plus ``Credential.key_id`` threading from
``PgAuthenticator.verify`` (the pool is faked — no live Postgres).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

from hivemind.domain import (
    ActorKind,
    AuditAction,
    AuditEvent,
    AuditFilters,
    key_fingerprint,
)
from hivemind.ports import Credential, Store
from hivemind.store.auth import PgAuthenticator, key_hash
from tests.fakes import make_clock, make_store


def _event(actor: str = "admin:abc", action: AuditAction = AuditAction.FLEET_CREATE) -> AuditEvent:
    return AuditEvent(
        actor_kind=ActorKind.ADMIN_KEY,
        actor=actor,
        action=action,
        target="data-eng",
        detail={"name": "data-eng"},
    )


async def test_memory_store_satisfies_the_store_port() -> None:
    assert isinstance(make_store(), Store)


async def test_record_audit_round_trips() -> None:
    store = make_store()
    stored = await store.record_audit(_event())
    assert stored.id
    assert stored.actor_kind is ActorKind.ADMIN_KEY
    assert stored.action is AuditAction.FLEET_CREATE
    assert stored.target == "data-eng"
    assert stored.detail == {"name": "data-eng"}
    assert await store.list_audit(AuditFilters(), limit=10) == [stored]


async def test_list_audit_is_newest_first_and_bounded() -> None:
    clock = make_clock()
    store = make_store(clock)
    first = await store.record_audit(_event(actor="a"))
    clock.advance_days(1)
    second = await store.record_audit(_event(actor="b"))
    clock.advance_days(1)
    third = await store.record_audit(_event(actor="c"))
    assert await store.list_audit(AuditFilters(), limit=10) == [third, second, first]
    assert await store.list_audit(AuditFilters(), limit=2) == [third, second]


async def test_list_audit_filters_by_actor_action_and_since() -> None:
    clock = make_clock()
    store = make_store(clock)
    old = await store.record_audit(_event(actor="a"))
    clock.advance_days(2)
    revoke = await store.record_audit(_event(actor="b", action=AuditAction.AGENT_REVOKE))
    newer = await store.record_audit(_event(actor="a"))

    assert await store.list_audit(AuditFilters(actor="a"), limit=10) == [newer, old]
    assert await store.list_audit(AuditFilters(action=AuditAction.AGENT_REVOKE), limit=10) == [
        revoke
    ]
    since = old.occurred_at + timedelta(days=1)
    assert await store.list_audit(AuditFilters(since=since), limit=10) == [newer, revoke]
    assert await store.list_audit(AuditFilters(actor="a", since=since), limit=10) == [newer]


# -- Credential.key_id (ADR 0027) ---------------------------------------------


def test_audit_actor_is_the_key_fingerprint_when_known() -> None:
    cred = Credential(user_id="admin", is_admin=True, key_id="0123456789ab")
    assert cred.audit_actor() == "admin:0123456789ab"


def test_audit_actor_falls_back_to_user_id_without_a_fingerprint() -> None:
    assert Credential(user_id="ops", is_admin=True).audit_actor() == "ops"


class _FakeConn:
    def __init__(self, row: dict[str, Any]) -> None:
        self._row = row
        self.looked_up: list[str] = []

    async def fetchrow(self, _sql: str, *args: Any) -> dict[str, Any] | None:
        self.looked_up.append(args[0])
        return self._row


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def _acquire(self):  # type: ignore[no-untyped-def]
        yield self._conn

    def acquire(self):  # type: ignore[no-untyped-def]
        return self._acquire()


async def test_verify_threads_the_key_fingerprint_into_the_credential() -> None:
    raw_key = "hm_some_admin_secret"
    conn = _FakeConn({"kind": "admin", "user_id": "admin", "agent_id": None, "agent_name": None})
    auth = PgAuthenticator("postgresql://example/db")
    with patch("hivemind.store.auth.make_pool", new=AsyncMock(return_value=_FakePool(conn))):
        credential = await auth.verify(raw_key)
    assert credential is not None and credential.is_admin
    # The fingerprint is the first 12 hex chars of the STORED hash (what
    # `hivemind-keys list` shows) — never the raw key or a prefix of it.
    assert credential.key_id == key_fingerprint(key_hash(raw_key))
    assert credential.key_id == key_hash(raw_key)[:12]
    assert raw_key not in credential.key_id
    assert credential.audit_actor() == f"admin:{key_hash(raw_key)[:12]}"
