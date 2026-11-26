"""Agents (ADRs 0011, 0012, 0028, 0045) on both adapters: roles, lifecycle, owner alias, renames and the listing."""

from __future__ import annotations

import pytest
from tests.integration.store_contract.helpers import T0, Adapter, _active, _agent_in, _people, draft

from hivemind.domain.access import (
    AgentNameTaken,
    AgentStatus,
    FleetBusy,
    InvalidAgentStatus,
    TrustLevel,
    UnknownAgent,
)
from hivemind.domain.agent_role import AgentRole
from hivemind.domain.feedback import Feedback, Verdict


async def test_set_and_clear_an_agent_role(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await store.register_agent("alice")
    pending_before, pending_after = await store.set_agent_role("alice", AgentRole.SCOUT)
    assert (pending_before.role, pending_after.role) == (None, AgentRole.SCOUT)
    await _agent_in(adapter, "bob", fleet.id)
    assert (await store.get_agent("alice")).role is AgentRole.SCOUT  # type: ignore[union-attr]
    assert (await store.get_agent("bob")).role is None  # type: ignore[union-attr]
    assert [a.role for a in await store.list_agents()] == [AgentRole.SCOUT, None]
    same_before, same_after = await store.set_agent_role("alice", AgentRole.SCOUT)
    assert same_before == same_after
    cleared_before, cleared_after = await store.set_agent_role("alice", None)
    assert (cleared_before.role, cleared_after.role) == (AgentRole.SCOUT, None)
    none_before, none_after = await store.set_agent_role("alice", None)
    assert none_before == none_after
    with pytest.raises(UnknownAgent):
        await store.set_agent_role("nobody", AgentRole.SCOUT)


async def test_a_revoked_agent_takes_no_role_and_a_role_it_had_can_be_cleared(
    adapter: Adapter,
) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await _agent_in(adapter, "alice", fleet.id)
    await _agent_in(adapter, "bob", fleet.id)
    await store.set_agent_role("alice", AgentRole.SCOUT)
    await store.revoke_agent("alice")
    await store.revoke_agent("bob")
    assert (await store.get_agent("alice")).role is AgentRole.SCOUT  # type: ignore[union-attr]
    with pytest.raises(InvalidAgentStatus):
        await store.set_agent_role("bob", AgentRole.SCOUT)
    assert (await store.get_agent("bob")).role is None  # type: ignore[union-attr]
    _, cleared = await store.set_agent_role("alice", None)
    assert cleared.role is None


async def test_agent_lifecycle(adapter: Adapter) -> None:
    store = adapter.store
    fleet = await store.create_fleet("blue")
    agent = await store.register_agent("Scout", "owner")
    assert (agent.status, agent.trust_level, agent.home_fleet_id) == (
        AgentStatus.PENDING,
        TrustLevel.UNTRUSTED,
        None,
    )
    assert agent.owner_alias == "owner"
    assert await store.register_agent("Scout", "someone else") == agent  # never overwritten
    assert (await store.register_agent("scout")).name == "Scout"  # case variant (ADR 0045)
    assert [a.name for a in await store.list_agents()] == ["Scout"]
    await store.register_agent("Bare")
    assert (await store.register_agent("Bare", "mallory")).owner_alias is None  # no back-fill
    active = await store.activate_agent(
        "Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
    )
    assert active.status is AgentStatus.ACTIVE and active.home_fleet_id == fleet.id
    assert active.activated_at is not None
    assert await store.get_agent("Scout") == active
    with pytest.raises(InvalidAgentStatus):
        await store.activate_agent(
            "Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
        )
    promoted = await store.set_agent_trust_level("Scout", TrustLevel.PRIVILEGED)
    assert promoted.trust_level is TrustLevel.PRIVILEGED
    demoted = await store.set_agent_trust_level("Scout", TrustLevel.UNTRUSTED)
    assert (demoted.trust_level, demoted.status) == (TrustLevel.UNTRUSTED, AgentStatus.ACTIVE)
    other = await store.create_fleet("red")
    assert (await store.set_agent_home_fleet("Scout", other.id)).home_fleet_id == other.id
    revoked = await store.revoke_agent("Scout")
    assert revoked.status is AgentStatus.REVOKED
    with pytest.raises(InvalidAgentStatus):
        await store.revoke_agent("Scout")
    again = await store.activate_agent(
        "Scout", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=fleet.id
    )
    assert (again.status, again.trust_level) == (AgentStatus.ACTIVE, TrustLevel.PRIVILEGED)
    assert (await store.revoke_agent("Bare")).status is AgentStatus.REVOKED  # even while pending
    for call in (
        store.revoke_agent("ghost"),
        store.set_agent_trust_level("ghost", TrustLevel.LURKER),
        store.set_agent_home_fleet("ghost", fleet.id),
        store.activate_agent("ghost", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id),
    ):
        with pytest.raises(UnknownAgent, match="unknown agent: ghost"):
            await call
    assert await store.get_agent("ghost") is None


async def test_set_agent_owner_alias_returns_before_and_after(adapter: Adapter) -> None:
    """ADR 0122: any status; (before, after) is read under one lock; the alias
    lives on the agent row only, and nothing else about the agent moves."""
    store = adapter.store
    fleet = await store.create_fleet("blue")
    await store.register_agent("Scout", "jane.doe")
    await store.register_agent("Bare")
    pending_before, pending_after = await store.set_agent_owner_alias("Scout", "jane.d")
    assert (pending_before.owner_alias, pending_after.owner_alias) == ("jane.doe", "jane.d")
    assert pending_after.status is AgentStatus.PENDING
    assert await store.get_agent("Scout") == pending_after
    await store.activate_agent("Scout", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id)
    active_before, active_after = await store.set_agent_owner_alias("Scout", "Jane.D")  # case only
    assert (active_before.owner_alias, active_after.owner_alias) == ("jane.d", "Jane.D")
    assert (active_after.status, active_after.trust_level, active_after.home_fleet_id) == (
        AgentStatus.ACTIVE,
        TrustLevel.CONTRIBUTOR,
        fleet.id,
    )
    same_before, same_after = await store.set_agent_owner_alias("Scout", "Jane.D")
    assert same_before == same_after
    await store.revoke_agent("Scout")  # a revoked agent can be corrected too
    assert (await store.set_agent_owner_alias("Scout", "jane"))[1].owner_alias == "jane"
    assert (await store.set_agent_owner_alias("Bare", "mallory"))[0].owner_alias is None
    assert [(a.name, a.owner_alias) for a in await store.list_agents()] == [
        ("Bare", "mallory"),
        ("Scout", "jane"),
    ]
    with pytest.raises(UnknownAgent, match="unknown agent: ghost"):
        await store.set_agent_owner_alias("ghost", "x")


async def test_rename_agent_rewrites_every_copy_of_the_name(adapter: Adapter) -> None:
    """ADR 0159: the entries it wrote, its ratings, pins and successions follow the new name; an
    ``agent`` label that was not the name stays as reported."""
    store = adapter.store
    red, _ = await _people(adapter)
    theirs = await store.create_entry(draft("heir's", author="heir", agent="heir"))
    mine = await store.create_entry(draft("mine", author="old", agent="old", scope="self"))
    labelled = await store.create_entry(draft("labelled", author="old", agent="claude-code"))
    shared = await store.create_entry(
        draft("shared", author="old", agent="old", scope="fleet", fleet_id=red)
    )
    await store.record_feedback(Feedback(theirs.id, "old", "old", Verdict.HELPFUL, updated_at=T0))
    await store.pin_entry(red, shared.id, "old", limit=5)

    before, after = await store.rename_agent("old", "older")

    assert (before.name, after.name) == ("old", "older")
    assert after.status is AgentStatus.ACTIVE and after.created_at == before.created_at
    assert await store.get_agent("old") is None
    assert await store.get_agent("older") == after
    stored = await store.get_entries([mine.id, labelled.id, shared.id])
    assert {(e.author, e.agent) for e in stored.values()} == {
        ("older", "older"),
        ("older", "claude-code"),
    }
    assert [(f.user, f.agent) for f in await store.list_feedback(theirs.id, 10)] == [
        ("older", "older")
    ]
    assert [p.pinned_by for p in await store.list_pins(red)] == ["older"]

    await store.retire_agent("older", "heir", "admin:1")
    await store.rename_agent("heir", "heir-2")
    succession = await store.get_succession("older")
    assert succession is not None and succession.successor == "heir-2"
    await store.rename_agent("older", "oldest")
    succession = await store.get_succession("oldest")
    assert succession is not None and succession.successor == "heir-2"
    assert await store.get_succession("older") is None
    assert await store.inherited_authors("heir-2") == ("oldest",)
    assert await store.former_agent_names(["oldest", "heir-2", "old"]) == {
        "oldest": ("old", "older"),
        "heir-2": ("heir",),
        "old": (),
    }


async def test_rename_agent_refuses_a_name_another_agent_holds_or_left(adapter: Adapter) -> None:
    store = adapter.store
    await store.register_agent("alice")
    await store.register_agent("bob")
    for taken in ("bob", "BOB", "Bob"):
        with pytest.raises(AgentNameTaken):
            await store.rename_agent("alice", taken)
    assert (await store.get_agent("alice")) is not None  # nothing moved

    await store.rename_agent("alice", "carol")
    for left in ("alice", "ALICE", "Alice"):
        with pytest.raises(AgentNameTaken):
            await store.rename_agent("bob", left)
    assert (await store.get_agent("bob")) is not None
    with pytest.raises(UnknownAgent, match="unknown agent: ghost"):
        await store.rename_agent("ghost", "casper")
    with pytest.raises(UnknownAgent):
        await store.rename_agent("alice", "dave")  # it is carol now


async def test_rename_agent_back_to_an_own_former_name_and_case_only(adapter: Adapter) -> None:
    store = adapter.store
    await store.register_agent("alice")
    await store.rename_agent("alice", "carol")
    assert await store.former_agent_names(["carol"]) == {"carol": ("alice",)}

    back_before, back = await store.rename_agent("carol", "alice")  # the name is its own again
    assert (back_before.name, back.name) == ("carol", "alice")
    assert await store.former_agent_names(["alice"]) == {"alice": ("carol",)}

    _, cased = await store.rename_agent("alice", "Alice")  # a case-only change is a rename
    assert cased.name == "Alice"
    assert await store.get_agent("alice") is None and await store.get_agent("Alice") == cased
    assert await store.former_agent_names(["Alice"]) == {"Alice": ("carol",)}

    same_before, same_after = await store.rename_agent("Alice", "Alice")
    assert same_before == same_after
    assert await store.former_agent_names(["Alice"]) == {"Alice": ("carol",)}


async def test_a_case_only_rename_reserves_nothing_and_a_later_rename_still_works(
    adapter: Adapter,
) -> None:
    """bob -> Bob -> x: the case-only step leaves no former name, so the next step's reservation
    cannot collide with it; both spellings stay unavailable throughout."""
    store = adapter.store
    await store.register_agent("bob", "jane")
    await store.rename_agent("bob", "Bob")
    assert await store.former_agent_names(["Bob"]) == {"Bob": ()}
    assert (await store.register_agent("BOB", "jane")).name == "Bob"  # still in use

    await store.rename_agent("Bob", "x")
    assert await store.former_agent_names(["x"]) == {"x": ("Bob",)}
    for left in ("bob", "BOB", "Bob"):
        assert (await store.register_agent(left, "jane")).name == "x"  # reserved, not registered
    assert [a.name for a in await store.list_agents()] == ["x"]
    await store.register_agent("y")
    with pytest.raises(AgentNameTaken):
        await store.rename_agent("y", "BOB")


async def test_a_name_an_agent_left_cannot_be_registered_again(adapter: Adapter) -> None:
    store = adapter.store
    await store.register_agent("alice", "jane")
    await store.rename_agent("alice", "carol")
    for left in ("alice", "ALICE"):
        holder = await store.register_agent(left, "jane")
        assert holder.name == "carol"  # the caller sees a different name: not registered
    assert [a.name for a in await store.list_agents()] == ["carol"]


async def test_a_write_under_a_name_that_was_renamed_is_refused(adapter: Adapter) -> None:
    """The window after a rename commits: a request still authenticated as the old name must not
    land an entry or a pin under it. The error is retryable (``FleetBusy``)."""
    store = adapter.store
    red = (await store.create_fleet("red")).id
    await _active(adapter, "alice", red)
    await store.rename_agent("alice", "carol")
    with pytest.raises(FleetBusy, match="renamed during this write"):
        await store.create_entry(draft("late", author="alice", scope="self"))
    kept = await store.create_entry(
        draft("fleet note", author="carol", scope="fleet", fleet_id=red)
    )
    with pytest.raises(FleetBusy, match="renamed during this write"):
        await store.pin_entry(red, kept.id, "alice", limit=5)
    # An author that is no registered agent (the admin key's) is not checked.
    await store.create_entry(draft("by admin", author="admin", agent="admin"))
    await store.rename_agent("carol", "alice")  # back: the name is current again
    await store.create_entry(draft("again", author="alice", scope="self"))


async def test_a_rating_under_a_name_that_was_renamed_is_refused(adapter: Adapter) -> None:
    """A rating filed under the old name would count twice and block renaming back (ADR 0160)."""
    store = adapter.store
    red = (await store.create_fleet("red")).id
    await _active(adapter, "alice", red)
    await _active(adapter, "zed", red)
    theirs = await store.create_entry(draft("zed's", author="zed", agent="zed"))
    await store.rename_agent("alice", "carol")
    late = Feedback(theirs.id, "alice", "alice", Verdict.HELPFUL, updated_at=T0)
    with pytest.raises(FleetBusy, match="renamed during this write"):
        await store.record_feedback(late)
    with pytest.raises(FleetBusy, match="renamed during this write"):
        await store.record_feedback_many([late])
    await store.record_feedback(Feedback(theirs.id, "carol", "carol", Verdict.WRONG))
    await store.record_feedback(Feedback(theirs.id, "admin", "admin", Verdict.HELPFUL))
    await store.rename_agent("carol", "alice")
    assert {(f.user, f.verdict) for f in await store.list_feedback(theirs.id, 10)} == {
        ("alice", Verdict.WRONG),
        ("admin", Verdict.HELPFUL),
    }


async def test_a_write_under_the_spelling_a_case_only_rename_left_is_refused(
    adapter: Adapter,
) -> None:
    store = adapter.store
    await _active(adapter, "bob", (await store.create_fleet("red")).id)
    await store.rename_agent("bob", "Bob")
    with pytest.raises(FleetBusy, match="renamed during this write"):
        await store.create_entry(draft("late", author="bob", scope="self"))


async def test_agents_list_by_name(adapter: Adapter) -> None:
    for name in ("zed", "amy"):
        await adapter.store.register_agent(name)
    assert [a.name for a in await adapter.store.list_agents()] == ["amy", "zed"]


async def test_agents_list_shows_no_key_use_for_an_agent_that_never_authenticated(
    adapter: Adapter,
) -> None:
    """ADR 0110: the agent list carries ``last_used_at``; the key check (the
    authenticator) is what stamps it, so a fresh or keyless agent shows none."""
    await adapter.store.register_agent("amy")
    assert [a.last_used_at for a in await adapter.store.list_agents()] == [None]
    got = await adapter.store.get_agent("amy")
    assert got is not None and got.last_used_at is None  # only the list fills it
    assert await adapter.store.agent_key_last_used("amy") is None
    assert await adapter.store.agent_key_last_used("ghost") is None


async def test_agents_list_shows_no_key_lock_for_an_agent_whose_key_was_never_locked(
    adapter: Adapter,
) -> None:
    """ADR 0117: the agent list carries ``key_locked_at`` (the key lock itself
    is the authenticator's); a fresh or keyless agent shows none, and only the
    list fills it."""
    await adapter.store.register_agent("amy")
    assert [a.key_locked_at for a in await adapter.store.list_agents()] == [None]
    got = await adapter.store.get_agent("amy")
    assert got is not None and got.key_locked_at is None
