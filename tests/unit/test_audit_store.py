"""``Credential.key_id`` threading from ``PgAuthenticator.verify`` into the
audit actor (ADR 0027; the pool is faked — no live Postgres). The audit log
at the ``Store`` seam is in ``tests/integration/test_store_contract.py``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

from hivemind.domain import (
    key_fingerprint,
)
from hivemind.ports import Credential
from hivemind.store.auth import PgAuthenticator, key_hash

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

    def acquire(self, **_kw):  # type: ignore[no-untyped-def]
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
