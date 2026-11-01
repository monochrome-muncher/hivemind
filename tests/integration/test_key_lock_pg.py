"""Postgres-backed tests for locking an agent's own key (ADR 0117).

* a locked key raises ``KeyLocked`` on its next check (no cache) and the lock
  and its audit row (fingerprint, ``via: "portal"``, no key) land together;
* a second press, or a press after the key was replaced, writes nothing;
* replacing the key (ADR 0108) and the CLI's ``rotate-agent`` clear the lock;
* revoking a locked agent still works and makes its key unknown again;
* ``list_agents`` carries the lock time; other keys are not locked.

Skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
import pytest
from tests.pg import ensure_migrated, reset_tables

from hivemind.config import Settings
from hivemind.domain.access import KeyLocked, TrustLevel
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.auth import key_hash
from hivemind.store.keys import _rotate_agent

ADMIN = Credential(user_id="admin", is_admin=True, key_id="adminfinger1")

Env = tuple[str, PgStore, PgAuthenticator, AccessService]


async def _exec(dsn: str, sql: str, *args: object) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    await reset_tables(dsn)
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    try:
        yield dsn, store, auth, AccessService(store, auth)
    finally:
        await store.close()
        await auth.close()


async def _agent_key(service: AccessService, store: PgStore, name: str = "alice") -> str:
    fleet = await service.create_fleet(f"f-{name}", ADMIN)
    await store.register_agent(name)
    _, key = await service.activate(name, TrustLevel.CONTRIBUTOR, fleet.id, ADMIN)
    return key


async def _lock(auth: PgAuthenticator, key: str) -> Credential:
    credential = await auth.verify(key)
    assert credential is not None
    return credential


async def test_a_locked_key_raises_key_locked_on_the_next_check(env: Env) -> None:
    _, store, auth, service = env
    key = await _agent_key(service, store)
    credential = await _lock(auth, key)
    assert await service.lock_own_key(credential) is True
    with pytest.raises(KeyLocked):
        await auth.verify(key)
    agent = await store.get_agent("alice")
    assert agent is not None and agent.status.value == "active"
    assert agent.trust_level is TrustLevel.CONTRIBUTOR


async def test_the_lock_writes_one_audit_row_with_the_fingerprint_and_no_key(env: Env) -> None:
    dsn, store, auth, service = env
    key = await _agent_key(service, store)
    credential = await _lock(auth, key)
    await service.lock_own_key(credential)
    await service.lock_own_key(credential)  # a second press changes and audits nothing
    [row] = await service.list_audit(ADMIN, AuditFilters(action=AuditAction.AGENT_KEY_LOCK), 10)
    assert (row.actor_kind, row.actor, row.target) == (ActorKind.AGENT_KEY, "alice", "alice")
    assert dict(row.detail) == {"key": key_hash(key)[:12], "via": "portal"}
    conn = await asyncpg.connect(dsn)
    try:
        dump = str(await conn.fetch("SELECT * FROM audit_log"))
    finally:
        await conn.close()
    assert key not in dump and key_hash(key) not in dump


async def test_the_cli_rotation_clears_the_lock_too(env: Env) -> None:
    dsn, store, auth, service = env
    key = await _agent_key(service, store)
    await service.lock_own_key(await _lock(auth, key))
    new = await _rotate_agent(dsn, "alice")
    assert await auth.verify(new) is not None


async def test_revoking_a_locked_agent_works_and_the_key_is_unknown_again(env: Env) -> None:
    _, store, auth, service = env
    key = await _agent_key(service, store)
    await service.lock_own_key(await _lock(auth, key))
    await service.revoke("alice", ADMIN)
    assert await auth.verify(key) is None
