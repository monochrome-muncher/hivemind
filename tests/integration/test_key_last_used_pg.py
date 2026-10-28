"""Postgres-backed tests for the key's last-use stamp (ADR 0110).

* a key check on a never-used key writes ``last_used_at``;
* a second check within the hour does not write (the row is not touched);
* a check after the hour writes again;
* a failing stamp never fails the key check;
* ``list_agents`` carries the agent's key's last use.

Skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import asyncpg
import pytest
from tests.pg import ensure_migrated

from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.auth import key_hash

TABLES = "TRUNCATE audit_log, credentials, agents, fleets, entries, feedbacks, pins, fleet_merges, fleet_merge_entries"
ADMIN = Credential(user_id="admin", is_admin=True)


async def _exec(dsn: str, sql: str, *args: object) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


async def _last_used(dsn: str, key: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchrow(
            "SELECT last_used_at, xmin::text AS xmin FROM credentials WHERE key_hash = $1",
            key_hash(key),
        )
    finally:
        await conn.close()


Env = tuple[str, PgStore, PgAuthenticator]


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    await _exec(dsn, TABLES)
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    try:
        yield dsn, store, auth
    finally:
        await store.close()
        await auth.close()
        await _exec(dsn, TABLES)


async def _agent_key(store: PgStore, auth: PgAuthenticator) -> str:
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", ADMIN)
    await store.register_agent("alice")
    _, key = await service.activate("alice", TrustLevel.LURKER, fleet.id, ADMIN)
    return key


async def test_first_check_stamps_and_a_second_within_the_hour_writes_nothing(env: Env) -> None:
    dsn, store, auth = env
    key = await _agent_key(store, auth)
    row = await _last_used(dsn, key)
    assert row is not None and row["last_used_at"] is None

    assert await auth.verify(key) is not None
    stamped = await _last_used(dsn, key)
    assert stamped is not None and stamped["last_used_at"] is not None

    # Not written again: same value and the same row version (xmin moves on any UPDATE).
    assert await auth.verify(key) is not None
    again = await _last_used(dsn, key)
    assert again == stamped


async def test_a_stale_stamp_is_refreshed(env: Env) -> None:
    dsn, store, auth = env
    key = await _agent_key(store, auth)
    await auth.verify(key)
    await _exec(
        dsn,
        "UPDATE credentials SET last_used_at = now() - interval '2 hours' WHERE key_hash = $1",
        key_hash(key),
    )
    old = await _last_used(dsn, key)
    assert await auth.verify(key) is not None
    new = await _last_used(dsn, key)
    assert old is not None and new is not None and new["last_used_at"] > old["last_used_at"]


async def test_admin_keys_are_stamped_too(env: Env) -> None:
    dsn, _, auth = env
    key = await auth.issue_admin_key()
    assert await auth.verify(key) is not None
    row = await _last_used(dsn, key)
    assert row is not None and row["last_used_at"] is not None


async def test_a_dead_key_is_not_stamped(env: Env) -> None:
    dsn, store, auth = env
    key = await _agent_key(store, auth)
    await AccessService(store, auth).revoke("alice", ADMIN)
    assert await auth.verify(key) is None
    # The revoke deleted the key row; nothing to stamp, nothing raised.
    assert await _last_used(dsn, key) is None


async def test_a_failing_stamp_does_not_fail_the_key_check(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, store, auth = env
    key = await _agent_key(store, auth)
    import hivemind.store.auth as auth_module

    monkeypatch.setattr(auth_module, "_last_warned", None)
    monkeypatch.setattr(auth_module, "_TOUCH_CREDENTIAL", "UPDATE no_such_table SET x = 1")
    credential = await auth.verify(key)
    assert credential is not None and credential.agent_name == "alice"
    assert "could not record last use" in caplog.text
    assert key not in caplog.text


async def test_list_agents_carries_the_keys_last_use(env: Env) -> None:
    _, store, auth = env
    key = await _agent_key(store, auth)
    await store.register_agent("bob")
    assert {a.name: a.last_used_at for a in await store.list_agents()} == {
        "alice": None,
        "bob": None,
    }
    await auth.verify(key)
    by_name = {a.name: a.last_used_at for a in await store.list_agents()}
    assert by_name["alice"] is not None and by_name["bob"] is None
    # ADR 0114: the same value, for one agent, is what whoami reports.
    assert await store.agent_key_last_used("alice") == by_name["alice"]
    assert await store.agent_key_last_used("bob") is None


async def test_a_locked_row_delays_the_check_by_about_a_second_not_longer(env: Env) -> None:
    dsn, store, auth = env
    key = await _agent_key(store, auth)
    holder = await asyncpg.connect(dsn)
    try:
        tx = holder.transaction()
        await tx.start()
        # Hold the key's row lock, as a long key-issue transaction would.
        await holder.execute(
            "UPDATE credentials SET created_at = created_at WHERE key_hash = $1", key_hash(key)
        )
        started = time.monotonic()
        credential = await asyncio.wait_for(auth.verify(key), timeout=10)
        elapsed = time.monotonic() - started
        await tx.rollback()
    finally:
        await holder.close()
    assert credential is not None and credential.agent_name == "alice"
    assert elapsed < 3.0
    row = await _last_used(dsn, key)
    assert row is not None and row["last_used_at"] is None  # the stamp gave up
