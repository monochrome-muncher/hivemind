"""An agent's last key use and the idle list (ADR 0110).

The key check stamps ``last_used_at`` at most once an hour per key; the admin
agent list shows it and says which active agents have been quiet for
``idle_agent_days``. Observation only: nothing here changes what a key may do.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from hivemind.api.deps import HivemindApp, create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import Agent, AgentStatus, TrustLevel, is_idle
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from tests.fakes import FakeAuthenticator, make_clock, make_embedder, make_search_config

ADMIN = Credential(user_id="admin", is_admin=True)
ORG = Credential(user_id="org", is_org=True, access_controlled=True)
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _agent(**kw: object) -> Agent:
    base: dict[str, object] = {
        "name": "a",
        "status": AgentStatus.ACTIVE,
        "trust_level": TrustLevel.LURKER,
        "created_at": NOW - timedelta(days=90),
        "activated_at": NOW - timedelta(days=80),
    }
    return Agent(**{**base, **kw})  # type: ignore[arg-type]


def test_idle_counts_from_last_use_else_activation() -> None:
    assert is_idle(_agent(last_used_at=NOW - timedelta(days=31)), NOW, 30)
    assert not is_idle(_agent(last_used_at=NOW - timedelta(days=29)), NOW, 30)
    # Never used: the clock runs from activation, so a new agent is not idle.
    assert is_idle(_agent(), NOW, 30)
    assert not is_idle(_agent(activated_at=NOW - timedelta(days=2)), NOW, 30)


def test_an_unused_key_counts_from_when_it_was_issued() -> None:
    # An old active agent that just got a new key is not idle.
    fresh = _agent(key_created_at=NOW - timedelta(days=1))
    assert not is_idle(fresh, NOW, 30)
    assert is_idle(_agent(key_created_at=NOW - timedelta(days=40)), NOW, 30)


async def test_a_freshly_issued_key_on_an_old_agent_is_not_idle() -> None:
    clock_now = [NOW]
    store = MemoryStore(make_clock(NOW - timedelta(days=90)))
    auth = FakeAuthenticator(store=store, clock=lambda: clock_now[0])
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", ADMIN)
    await service.register("alice", ORG)
    await service.activate("alice", TrustLevel.LURKER, fleet.id, ADMIN)
    (agent,) = await store.list_agents()
    assert agent.last_used_at is None and agent.key_created_at == NOW
    assert not is_idle(agent, NOW + timedelta(days=5), 30)
    assert is_idle(agent, NOW + timedelta(days=31), 30)


async def test_only_the_admin_list_carries_key_facts() -> None:
    store, auth, key, _ = await _active()
    await auth.verify(key)
    assert (await store.list_agents())[0].last_used_at is not None
    got = await store.get_agent("alice")
    assert got is not None and got.last_used_at is None and got.key_created_at is None


def test_pending_and_revoked_agents_are_never_idle() -> None:
    for status in (AgentStatus.PENDING, AgentStatus.REVOKED):
        assert not is_idle(_agent(status=status, last_used_at=None), NOW, 30)


async def _active() -> tuple[MemoryStore, FakeAuthenticator, str, str]:
    store = MemoryStore(make_clock())
    auth = FakeAuthenticator(store=store)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", ADMIN)
    await service.register("alice", ORG)
    _, key = await service.activate("alice", TrustLevel.LURKER, fleet.id, ADMIN)
    return store, auth, key, fleet.id


async def test_a_key_check_records_use_and_the_list_shows_it() -> None:
    store, auth, key, _ = await _active()
    assert (await store.list_agents())[0].last_used_at is None
    assert await auth.verify(key) is not None
    assert (await store.list_agents())[0].last_used_at is not None


async def test_a_second_check_within_the_hour_does_not_write_again() -> None:
    clock_now = [NOW]
    store = MemoryStore(make_clock())
    auth = FakeAuthenticator(store=store, clock=lambda: clock_now[0])
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", ADMIN)
    await service.register("alice", ORG)
    _, key = await service.activate("alice", TrustLevel.LURKER, fleet.id, ADMIN)

    await auth.verify(key)
    assert auth.use_writes == 1
    clock_now[0] = NOW + timedelta(minutes=59)
    await auth.verify(key)
    await auth.verify(key)
    assert auth.use_writes == 1
    assert (await store.list_agents())[0].last_used_at == NOW
    clock_now[0] = NOW + timedelta(minutes=61)
    await auth.verify(key)
    assert auth.use_writes == 2


async def test_a_refused_key_records_nothing() -> None:
    store, auth, key, _ = await _active()
    await AccessService(store, auth).revoke("alice", ADMIN)
    assert await auth.verify(key) is None
    assert auth.use_writes == 0
    assert (await store.list_agents())[0].last_used_at is None


def _app(days: int = 30) -> HivemindApp:
    return create_app_for_config(
        Settings(idle_agent_days=days),
        store=MemoryStore(make_clock()),
        embedder=make_embedder(),
        authenticator=FakeAuthenticator(),
        search_config=make_search_config(),
    )


async def test_admin_agents_lists_last_used_and_idle() -> None:
    app = _app(days=30)
    store = app.store
    assert isinstance(store, MemoryStore)
    fleet = await store.create_fleet("f")
    for name in ("busy", "quiet", "never"):
        await store.register_agent(name)
        await store.activate_agent(name, trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    await store.register_agent("waiting")
    now = datetime.now(UTC)
    store.record_key_use("busy", now - timedelta(days=1))
    store.record_key_use("quiet", now - timedelta(days=45))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://hivemind.test"
    ) as client:
        resp = await client.get("/v1/admin/agents", headers={"X-API-Key": "hm_admin"})
    rows = {r["name"]: r for r in resp.json()}
    assert resp.status_code == 200
    assert rows["busy"]["idle"] is False and rows["busy"]["last_used_at"] is not None
    assert rows["quiet"]["idle"] is True
    assert rows["never"]["last_used_at"] is None and rows["never"]["idle_after_days"] == 30
    assert rows["waiting"]["idle"] is False


async def test_a_non_admin_cannot_list_agents() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(_app())), base_url="http://hivemind.test"
    ) as client:
        resp = await client.get("/v1/admin/agents", headers={"X-API-Key": "hm_org"})
    assert resp.status_code == 403


@pytest.mark.parametrize("value", [0, -1])
def test_idle_agent_days_must_be_positive(value: int) -> None:
    with pytest.raises(ValueError):
        Settings(idle_agent_days=value)
