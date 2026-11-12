"""The ``Authenticator`` port contract, run against both adapters.

``FakeAuthenticator`` re-implements the status gate, key replacement, the
portal key lock and the once-an-hour last-use stamp, and many unit tests
lean on that. These cases hold it to what ``PgAuthenticator`` does, the
way ``test_store_contract.py`` does for ``MemoryStore``: each case goes
through the ``Authenticator`` port only, and moves an agent's status
through the ``Store``.

``fake`` always runs; ``pg`` needs the dev Postgres (it skips when the
database is down, and CI's ``HM_TEST_REQUIRE_PG=1`` turns that skip into a
failure). A divergence is marked ``xfail(strict=True)`` on the adapter that
differs, so fixing it turns the mark into a failure that asks for it to go.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from tests.fakes import FakeAuthenticator
from tests.pg import ensure_migrated, reset_tables

from hivemind.config import Settings
from hivemind.domain.access import (
    AgentStatus,
    InvalidAgentStatus,
    KeyLocked,
    StoreUnavailable,
    TrustLevel,
    UnknownAgent,
)
from hivemind.domain.audit import ActorKind, AuditAction, AuditFilters
from hivemind.memstore import MemoryStore
from hivemind.ports import Authenticator, Store
from hivemind.store import PgAuthenticator, PgStore

T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)

_REPLACE = {"audit_actor_kind": ActorKind.ADMIN_KEY, "audit_actor": "adminfinger1"}


class AdvanceableClock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Adapter:
    kind: str
    auth: Authenticator
    store: Store
    # Make the agent's stored last-use stamp ``minutes`` older: the fake reads
    # its injected clock, Postgres stamps with the database clock, so there the
    # stored timestamp is moved back instead.
    age_stamp: Callable[[str, int], Awaitable[None]]
    home_fleet: str = ""

    async def agent(self, name: str, status: AgentStatus = AgentStatus.ACTIVE) -> None:
        """Register ``name`` and bring it to ``status`` through the Store."""
        await self.store.register_agent(name)
        if status is AgentStatus.PENDING:
            return
        if not self.home_fleet:
            self.home_fleet = (await self.store.create_fleet("home")).id
        await self.store.activate_agent(
            name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=self.home_fleet
        )
        if status is AgentStatus.REVOKED:
            await self.store.revoke_agent(name)

    async def key(self, name: str = "alice") -> str:
        await self.agent(name)
        return await self.auth.issue_agent_key(name)

    async def key_id(self, key: str) -> str:
        credential = await self.auth.verify(key)
        assert credential is not None and credential.key_id is not None
        return credential.key_id

    async def last_used(self, name: str = "alice") -> datetime | None:
        return await self.store.agent_key_last_used(name)


async def _pg_adapter() -> AsyncIterator[Adapter]:
    settings = Settings()
    dsn, dim = settings.database_url, settings.embedding_dim
    await ensure_migrated(dsn, dim)

    async def sql(statement: str, *args: object) -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(statement, *args)
        finally:
            await conn.close()

    async def age_stamp(name: str, minutes: int) -> None:
        await sql(
            "UPDATE credentials SET last_used_at = last_used_at - make_interval(mins => $2) "
            "WHERE agent_name = $1",
            name,
            minutes,
        )

    await reset_tables(dsn)
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    try:
        yield Adapter("pg", auth, store, age_stamp)
    finally:
        await store.close()
        await auth.close()


@pytest.fixture(params=["fake", "pg"])
async def adapter(request: pytest.FixtureRequest) -> AsyncIterator[Adapter]:
    if request.param == "pg":
        async for pg in _pg_adapter():
            yield pg
        return
    clock = AdvanceableClock()
    store = MemoryStore(clock=clock)

    async def age_stamp(_name: str, minutes: int) -> None:
        clock.now += timedelta(minutes=minutes)

    yield Adapter("fake", FakeAuthenticator(store=store, clock=clock), store, age_stamp)


# -- issue / verify ---------------------------------------------------------------


async def test_an_issued_key_verifies_to_its_agent_with_a_fingerprint(adapter: Adapter) -> None:
    key = await adapter.key()
    credential = await adapter.auth.verify(key)
    assert credential is not None
    assert credential.agent_name == "alice"
    assert credential.key_id is not None and len(credential.key_id) == 12


async def test_a_made_up_key_verifies_to_none(adapter: Adapter) -> None:
    await adapter.key()
    assert await adapter.auth.verify("hm_not_a_real_key") is None


async def test_issuing_for_an_unknown_agent_raises(adapter: Adapter) -> None:
    with pytest.raises(UnknownAgent):
        await adapter.auth.issue_agent_key("ghost")


@pytest.mark.parametrize("status", [AgentStatus.PENDING, AgentStatus.REVOKED])
async def test_issuing_for_a_non_active_agent_raises(adapter: Adapter, status: AgentStatus) -> None:
    await adapter.agent("alice", status)
    with pytest.raises(InvalidAgentStatus):
        await adapter.auth.issue_agent_key("alice")


async def test_a_key_stops_verifying_once_its_agent_is_revoked(adapter: Adapter) -> None:
    key = await adapter.key()
    assert await adapter.auth.verify(key) is not None
    await adapter.store.revoke_agent("alice")
    assert await adapter.auth.verify(key) is None


async def test_issuing_again_replaces_the_key(adapter: Adapter) -> None:
    old = await adapter.key()
    new = await adapter.auth.issue_agent_key("alice")
    assert await adapter.auth.verify(old) is None
    assert await adapter.auth.verify(new) is not None


# -- credential without a key (ADR 0136) ------------------------------------------


async def test_a_credential_built_without_a_key_is_what_verify_returns_minus_the_fingerprint(
    adapter: Adapter,
) -> None:
    await adapter.agent("old")
    key = await adapter.key("alice")
    await adapter.store.retire_agent("old", "alice", "admin")
    verified = await adapter.auth.verify(key)
    assert verified is not None and verified.inherits_from == ("old",)
    built = await adapter.auth.credential_for_agent("alice")
    assert built == replace(verified, key_id=None)


async def test_a_credential_built_without_a_key_ignores_a_key_lock(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.auth.lock_agent_key("alice", key_id=await adapter.key_id(key))
    built = await adapter.auth.credential_for_agent("alice")
    assert built is not None and built.agent_name == "alice"


@pytest.mark.parametrize("status", [AgentStatus.PENDING, AgentStatus.REVOKED])
async def test_no_credential_is_built_for_a_non_active_agent(
    adapter: Adapter, status: AgentStatus
) -> None:
    await adapter.agent("alice", status)
    assert await adapter.auth.credential_for_agent("alice") is None


async def test_no_credential_is_built_for_an_unknown_agent(adapter: Adapter) -> None:
    assert await adapter.auth.credential_for_agent("ghost") is None


# -- replace ----------------------------------------------------------------------


async def test_replace_kills_the_old_key_and_the_new_one_verifies(adapter: Adapter) -> None:
    old = await adapter.key()
    replacement = await adapter.auth.replace_agent_key("alice", **_REPLACE)
    assert replacement.raw_key != old
    assert await adapter.auth.verify(old) is None
    credential = await adapter.auth.verify(replacement.raw_key)
    assert credential is not None and credential.agent_name == "alice"


async def test_replace_fingerprints_name_the_old_and_the_new_key(adapter: Adapter) -> None:
    old = await adapter.key()
    old_id = await adapter.key_id(old)
    replacement = await adapter.auth.replace_agent_key("alice", **_REPLACE)
    assert replacement.old_key_id == old_id
    assert replacement.new_key_id == await adapter.key_id(replacement.raw_key)
    assert replacement.old_key_id != replacement.new_key_id


async def test_replace_writes_one_audit_row_without_a_key(adapter: Adapter) -> None:
    old = await adapter.key()
    replacement = await adapter.auth.replace_agent_key("alice", **_REPLACE)
    [row] = await adapter.store.list_audit(AuditFilters(action=AuditAction.AGENT_KEY_REPLACE), 10)
    assert (row.actor_kind, row.actor, row.target) == (
        ActorKind.ADMIN_KEY,
        "adminfinger1",
        "alice",
    )
    assert dict(row.detail) == {
        "old_key": replacement.old_key_id,
        "new_key": replacement.new_key_id,
    }
    assert old not in str(row) and replacement.raw_key not in str(row)


async def test_replace_for_an_unknown_or_non_active_agent_raises(adapter: Adapter) -> None:
    await adapter.agent("pending", AgentStatus.PENDING)
    with pytest.raises(UnknownAgent):
        await adapter.auth.replace_agent_key("ghost", **_REPLACE)
    with pytest.raises(InvalidAgentStatus):
        await adapter.auth.replace_agent_key("pending", **_REPLACE)


# -- lock -------------------------------------------------------------------------


async def test_lock_with_the_wrong_fingerprint_changes_nothing(adapter: Adapter) -> None:
    key = await adapter.key()
    assert await adapter.auth.lock_agent_key("alice", key_id="0" * 12) is False
    assert await adapter.auth.verify(key) is not None


async def test_lock_with_the_right_fingerprint_makes_verify_raise(adapter: Adapter) -> None:
    key = await adapter.key()
    assert await adapter.auth.lock_agent_key("alice", key_id=await adapter.key_id(key)) is True
    with pytest.raises(KeyLocked):
        await adapter.auth.verify(key)


async def test_locking_twice_returns_false(adapter: Adapter) -> None:
    key_id = await adapter.key_id(await adapter.key())
    assert await adapter.auth.lock_agent_key("alice", key_id=key_id) is True
    assert await adapter.auth.lock_agent_key("alice", key_id=key_id) is False


async def test_lock_writes_one_audit_row_and_a_repeat_writes_none(adapter: Adapter) -> None:
    key_id = await adapter.key_id(await adapter.key())
    await adapter.auth.lock_agent_key("alice", key_id=key_id)
    await adapter.auth.lock_agent_key("alice", key_id=key_id)
    [row] = await adapter.store.list_audit(AuditFilters(action=AuditAction.AGENT_KEY_LOCK), 10)
    assert (row.actor_kind, row.actor, row.target) == (ActorKind.AGENT_KEY, "alice", "alice")
    assert dict(row.detail) == {"key": key_id, "via": "portal"}


async def test_lock_for_a_replaced_key_locks_nothing(adapter: Adapter) -> None:
    stale_id = await adapter.key_id(await adapter.key())
    new = (await adapter.auth.replace_agent_key("alice", **_REPLACE)).raw_key
    assert await adapter.auth.lock_agent_key("alice", key_id=stale_id) is False
    assert await adapter.auth.verify(new) is not None


async def test_lock_for_a_reissued_key_locks_nothing(adapter: Adapter) -> None:
    stale_id = await adapter.key_id(await adapter.key())
    new = await adapter.auth.issue_agent_key("alice")
    assert await adapter.auth.lock_agent_key("alice", key_id=stale_id) is False
    assert await adapter.auth.verify(new) is not None


async def test_replacing_a_locked_key_clears_the_lock(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.auth.lock_agent_key("alice", key_id=await adapter.key_id(key))
    replacement = await adapter.auth.replace_agent_key("alice", **_REPLACE)
    assert await adapter.auth.verify(replacement.raw_key) is not None
    assert await adapter.auth.verify(key) is None
    assert [a.key_locked_at for a in await adapter.store.list_agents()] == [None]


async def test_issuing_again_clears_the_lock(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.auth.lock_agent_key("alice", key_id=await adapter.key_id(key))
    assert await adapter.auth.verify(await adapter.auth.issue_agent_key("alice")) is not None


async def test_the_lock_time_is_on_the_locked_agents_record_only(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.key("bob")
    await adapter.auth.lock_agent_key("alice", key_id=await adapter.key_id(key))
    locked = {a.name: a.key_locked_at for a in await adapter.store.list_agents()}
    assert locked["alice"] is not None and locked["bob"] is None


# -- revoke / rotate / admin ------------------------------------------------------


async def test_revoke_agent_key_kills_the_key_for_good(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.store.revoke_agent("alice")
    await adapter.auth.revoke_agent_key("alice")
    # Re-activating the agent must not bring the old key back.
    await adapter.store.activate_agent(
        "alice", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=adapter.home_fleet
    )
    assert await adapter.auth.verify(key) is None


async def test_rotating_the_org_key_kills_the_old_one(adapter: Adapter) -> None:
    first = await adapter.auth.rotate_org_key()
    credential = await adapter.auth.verify(first)
    assert credential is not None and credential.is_org
    second = await adapter.auth.rotate_org_key()
    assert second != first
    assert await adapter.auth.verify(first) is None
    credential = await adapter.auth.verify(second)
    assert credential is not None and credential.is_org


async def test_an_issued_admin_key_verifies_as_admin(adapter: Adapter) -> None:
    key = await adapter.auth.issue_admin_key()
    credential = await adapter.auth.verify(key)
    assert credential is not None and credential.is_admin
    assert credential.key_id is not None and len(credential.key_id) == 12


# -- last use (ADR 0110) ----------------------------------------------------------


async def test_the_first_verify_stamps_the_last_use(adapter: Adapter) -> None:
    key = await adapter.key()
    assert await adapter.last_used() is None
    await adapter.auth.verify(key)
    stamped = await adapter.last_used()
    assert stamped is not None
    by_name = {a.name: a.last_used_at for a in await adapter.store.list_agents()}
    assert by_name["alice"] == stamped


async def test_a_second_verify_within_the_hour_does_not_move_the_stamp(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.auth.verify(key)
    await adapter.age_stamp("alice", 30)  # time passes, inside the hour
    aged = await adapter.last_used()
    await adapter.auth.verify(key)
    assert await adapter.last_used() == aged


async def test_a_verify_after_the_hour_stamps_again(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.auth.verify(key)
    await adapter.age_stamp("alice", 120)
    before = await adapter.last_used()
    await adapter.auth.verify(key)
    after = await adapter.last_used()
    assert before is not None and after is not None and after > before


async def test_a_locked_key_does_not_move_the_stamp(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.auth.lock_agent_key("alice", key_id=await adapter.key_id(key))
    await adapter.age_stamp("alice", 180)
    before = await adapter.last_used()
    with pytest.raises(KeyLocked):
        await adapter.auth.verify(key)
    assert await adapter.last_used() == before


async def test_a_replaced_key_starts_unused(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.auth.verify(key)
    await adapter.auth.replace_agent_key("alice", **_REPLACE)
    assert await adapter.last_used() is None


async def test_a_dead_key_is_not_stamped(adapter: Adapter) -> None:
    key = await adapter.key()
    await adapter.store.revoke_agent("alice")
    assert await adapter.auth.verify(key) is None
    assert await adapter.last_used() is None


# -- successions (ADR 0113) -------------------------------------------------------


async def test_the_successor_inherits_the_retired_agents_authorship(adapter: Adapter) -> None:
    await adapter.agent("old")
    key = await adapter.key("new")
    await adapter.store.retire_agent("old", "new", "admin")
    credential = await adapter.auth.verify(key)
    assert credential is not None
    assert credential.inherits_from == ("old",)
    assert credential.owned_authors == ("new", "old")


async def test_pg_verify_on_an_unreachable_database_raises_store_unavailable() -> None:
    """A refused connection is a retryable store failure, never a raw driver
    error (a 500) and never ``None`` (a 401)."""
    auth = PgAuthenticator("postgresql://hivemind:hivemind@127.0.0.1:1/hivemind")
    try:
        with pytest.raises(StoreUnavailable) as excinfo:
            await auth.verify("hm_whatever")
    finally:
        await auth.close()
    assert "hm_whatever" not in str(excinfo.value)


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (asyncpg.exceptions.ConnectionDoesNotExistError(), True),
        (asyncpg.exceptions.CannotConnectNowError(), True),
        (asyncpg.exceptions.TooManyConnectionsError(), True),
        (asyncpg.InterfaceError("closed"), True),
        (ConnectionResetError(), True),
        (asyncpg.exceptions.InvalidPasswordError(), False),
        (asyncpg.exceptions.UndefinedTableError(), False),
        (asyncpg.exceptions.InvalidCatalogNameError(), False),
    ],
)
async def test_pg_key_and_agent_lookups_map_only_transient_failures_to_store_unavailable(
    error: BaseException, transient: bool
) -> None:
    """A permanent fault (wrong password, unmigrated pool, missing database)
    keeps surfacing as itself; only a connection-level failure is retryable."""

    class _Boom:
        def acquire(self, **_kwargs: object) -> object:
            raise error

    auth = PgAuthenticator("postgresql://unused")
    auth._pool = _Boom()  # type: ignore[assignment]
    expected = StoreUnavailable if transient else type(error)
    with pytest.raises(expected) as excinfo:
        await auth.verify("hm_whatever")
    if not transient:
        assert not isinstance(excinfo.value, StoreUnavailable)
    with pytest.raises(expected):
        await auth.credential_for_agent("alice")
