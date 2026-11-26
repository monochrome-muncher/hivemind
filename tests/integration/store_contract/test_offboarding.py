"""Offboarding an owner (ADR 0135) on both adapters."""

from __future__ import annotations

from dataclasses import replace

import asyncpg
import pytest
from tests.integration.store_contract.helpers import Adapter, _people

from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, InvalidAgentStatus, TrustLevel, UnknownAgent
from hivemind.domain.offboarding import OffboardAction, OffboardChanged, OffboardMode, UnknownOwner
from hivemind.domain.succession import SuccessionRefused
from hivemind.domain.validation import InvalidInput


async def _leaver(a: Adapter, name: str, alias: str, *, active: bool = True) -> None:
    await a.store.register_agent(name, alias)
    if active:
        red = (await a.store.list_fleets())[0].id
        await a.store.activate_agent(name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=red)


async def _offboarding_world(a: Adapter) -> None:
    await _people(a)  # old (red) and heir (blue), no owner alias
    await a.store.set_agent_owner_alias("old", "jane")
    await _leaver(a, "jane-2", "jane")
    await _leaver(a, "jane-wait", "jane", active=False)
    await _leaver(a, "jane-gone", "jane")
    await a.store.revoke_agent("jane-gone")
    await _leaver(a, "jane-prior", "jane")
    await a.store.retire_agent("jane-prior", "heir", "x")


async def _states(a: Adapter, *names: str) -> dict[str, AgentStatus]:
    return {n: (await a.store.get_agent(n)).status for n in names}  # type: ignore[union-attr]


async def test_offboard_owner_retires_every_agent_in_one_step(adapter: Adapter) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    steps = await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "admin:1")
    assert [(s.name, s.action) for s in steps] == [
        ("jane-2", OffboardAction.RETIRE),
        ("jane-gone", OffboardAction.RETIRE),
        ("jane-prior", OffboardAction.SKIP),
        ("jane-wait", OffboardAction.REJECT),
        ("old", OffboardAction.RETIRE),
    ]
    assert steps[0].was is AgentStatus.ACTIVE and steps[1].was is AgentStatus.REVOKED
    assert steps[2].reason is not None and "already retired" in steps[2].reason
    assert sorted(await store.inherited_authors("heir")) == [
        "jane-2",
        "jane-gone",
        "jane-prior",
        "old",
    ]
    assert set((await _states(adapter, "jane-2", "jane-wait", "old")).values()) == {
        AgentStatus.REVOKED
    }
    assert (await store.get_agent("heir")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert {s.by for s in await store.list_successions()} == {"admin:1", "x"}


async def test_offboard_owner_revoke_revokes_active_and_rejects_pending(
    adapter: Adapter,
) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    steps = await store.offboard_owner("jane", OffboardMode.REVOKE, None, "admin:1")
    assert {s.name: s.action for s in steps} == {
        "jane-2": OffboardAction.REVOKE,
        "old": OffboardAction.REVOKE,
        "jane-wait": OffboardAction.REJECT,
        "jane-gone": OffboardAction.SKIP,
        "jane-prior": OffboardAction.SKIP,
    }
    states = await _states(adapter, "jane-2", "old", "jane-wait")
    assert set(states.values()) == {AgentStatus.REVOKED}
    assert [s.retired_agent for s in await store.list_successions()] == ["jane-prior"]


async def test_offboard_owner_is_case_sensitive_and_needs_an_owner(adapter: Adapter) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    with pytest.raises(UnknownOwner):
        await store.offboard_owner("Jane", OffboardMode.REVOKE, None, "x")
    with pytest.raises(UnknownOwner):
        await store.offboard_owner("nobody", OffboardMode.REVOKE, None, "x")


async def test_offboard_owner_successor_refusals_change_nothing(adapter: Adapter) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    with pytest.raises(UnknownAgent):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "ghost", "x")
    with pytest.raises(InvalidAgentStatus):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "jane-wait", "x")
    with pytest.raises(SuccessionRefused, match="another owner"):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "jane-2", "x")
    with pytest.raises(InvalidInput):
        await store.offboard_owner("jane", OffboardMode.RETIRE, None, "x")
    with pytest.raises(InvalidInput):
        await store.offboard_owner("jane", OffboardMode.REVOKE, "heir", "x")
    states = await _states(adapter, "jane-2", "old", "jane-wait", "jane-gone")
    assert states["jane-2"] is AgentStatus.ACTIVE and states["jane-wait"] is AgentStatus.PENDING
    assert [s.retired_agent for s in await store.list_successions()] == ["jane-prior"]


async def test_offboard_owner_is_all_or_nothing_when_one_agent_would_cycle(
    adapter: Adapter,
) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    await store.retire_agent("heir", "old", "x")  # heir hands down to old, an agent of jane
    # Re-activate behind the guard (an older pod could): heir would then succeed what it inherits.
    if adapter.kind == "memory":
        agents = store._agents  # type: ignore[attr-defined]
        agents["heir"] = replace(agents["heir"], status=AgentStatus.ACTIVE)
    else:
        conn = await asyncpg.connect(Settings().database_url)
        try:
            await conn.execute("UPDATE agents SET status = 'active' WHERE name = 'heir'")
        finally:
            await conn.close()
    with pytest.raises(SuccessionRefused, match="cycle"):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "x")
    states = await _states(adapter, "old", "jane-2", "jane-wait", "jane-gone")
    assert states["old"] is AgentStatus.ACTIVE and states["jane-2"] is AgentStatus.ACTIVE
    assert states["jane-wait"] is AgentStatus.PENDING and states["jane-gone"] is AgentStatus.REVOKED
    assert {s.retired_agent for s in await store.list_successions()} == {"jane-prior", "heir"}


async def test_offboard_owner_refuses_a_plan_that_differs_from_the_expected_one(
    adapter: Adapter,
) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    wrong = {"old": "retire", "jane-2": "retire", "jane-gone": "revoke"}
    with pytest.raises(OffboardChanged, match="jane-gone"):
        await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "x", wrong)
    assert (await store.get_agent("old")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert [s.retired_agent for s in await store.list_successions()] == ["jane-prior"]
    shown = {
        "jane-2": "retire",
        "jane-gone": "retire",
        "jane-prior": "skip",
        "jane-wait": "reject",
        "old": "retire",
    }
    steps = await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "x", shown)
    assert {s.name: s.action.value for s in steps} == shown


async def test_offboard_owner_revokes_an_active_agent_that_is_also_retired(
    adapter: Adapter,
) -> None:
    store = adapter.store
    await _offboarding_world(adapter)
    # An older pod re-activated jane-prior behind the guard: its key works, its succession stands.
    if adapter.kind == "memory":
        agents = store._agents  # type: ignore[attr-defined]
        agents["jane-prior"] = replace(agents["jane-prior"], status=AgentStatus.ACTIVE)
    else:
        conn = await asyncpg.connect(Settings().database_url)
        try:
            await conn.execute("UPDATE agents SET status = 'active' WHERE name = 'jane-prior'")
        finally:
            await conn.close()
    steps = await store.offboard_owner("jane", OffboardMode.RETIRE, "heir", "later")
    prior = next(s for s in steps if s.name == "jane-prior")
    assert prior.action is OffboardAction.REVOKE and prior.was is AgentStatus.ACTIVE
    assert (await store.get_agent("jane-prior")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    assert (await store.get_succession("jane-prior")).by == "x"  # type: ignore[union-attr]
    # In revoke mode the same agent is revoked too.
    await _leaver(adapter, "jane-4", "jane")
    await store.retire_agent("jane-4", "heir", "x")
    if adapter.kind == "memory":
        agents["jane-4"] = replace(agents["jane-4"], status=AgentStatus.ACTIVE)
    else:
        conn = await asyncpg.connect(Settings().database_url)
        try:
            await conn.execute("UPDATE agents SET status = 'active' WHERE name = 'jane-4'")
        finally:
            await conn.close()
    steps = await store.offboard_owner("jane", OffboardMode.REVOKE, None, "later")
    assert {s.name: s.action for s in steps}["jane-4"] is OffboardAction.REVOKE


async def test_offboard_owner_refuses_revoke_with_a_successor_before_any_lookup(
    adapter: Adapter,
) -> None:
    with pytest.raises(InvalidInput):
        await adapter.store.offboard_owner("nobody", OffboardMode.REVOKE, "ghost", "x")
