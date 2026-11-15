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

import asyncpg
import pytest

from hivemind.domain.access import TrustLevel
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.auth import key_hash

ADMIN = Credential(user_id="admin", is_admin=True)


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


async def _agent_key(store: PgStore, auth: PgAuthenticator) -> str:
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", ADMIN)
    await store.register_agent("alice")
    _, key = await service.activate("alice", TrustLevel.LURKER, fleet.id, ADMIN)
    return key


async def test_first_check_stamps_and_a_second_within_the_hour_writes_nothing(pool: Env) -> None:
    dsn, store, auth = pool
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


async def test_admin_keys_are_stamped_too(pool: Env) -> None:
    dsn, _, auth = pool
    key = await auth.issue_admin_key()
    assert await auth.verify(key) is not None
    row = await _last_used(dsn, key)
    assert row is not None and row["last_used_at"] is not None


async def test_a_failing_stamp_does_not_fail_the_key_check(
    pool: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, store, auth = pool
    key = await _agent_key(store, auth)
    import hivemind.store.auth as auth_module

    monkeypatch.setattr(auth_module, "_last_warned", None)
    monkeypatch.setattr(auth_module, "_TOUCH_CREDENTIAL", "UPDATE no_such_table SET x = 1")
    credential = await auth.verify(key)
    assert credential is not None and credential.agent_name == "alice"
    assert "could not record last use" in caplog.text
    assert key not in caplog.text


async def test_list_agents_carries_the_keys_last_use(pool: Env) -> None:
    _, store, auth = pool
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


async def test_a_locked_row_delays_the_check_by_about_a_second_not_longer(pool: Env) -> None:
    dsn, store, auth = pool
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
