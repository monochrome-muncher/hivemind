"""Successions (ADR 0113) on both adapters."""

from __future__ import annotations

from dataclasses import replace

import asyncpg
import pytest
from tests.integration.store_contract.helpers import T0, Adapter, _active, _people, write

from hivemind.config import Settings
from hivemind.domain.access import (
    AgentStatus,
    InvalidAgentStatus,
    TrustLevel,
    UnknownAgent,
    Visibility,
)
from hivemind.domain.entry import EntryFilters
from hivemind.domain.feedback import Feedback, FeedbackLogFilters, Verdict
from hivemind.domain.pin import PinRefusal, PinWrite
from hivemind.domain.succession import AgentRetired, SuccessionRefused, UnknownSuccession
from hivemind.domain.validation import InvalidInput


async def test_retire_agent_records_the_succession_and_revokes_the_agent(adapter: Adapter) -> None:
    store = adapter.store
    await _people(adapter)
    succession = await store.retire_agent("old", "heir", "admin:1")
    assert (succession.retired_agent, succession.successor, succession.by) == (
        "old",
        "heir",
        "admin:1",
    )
    assert succession.at is not None
    assert (await store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    assert (await store.get_agent("heir")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert await store.get_succession("old") == succession
    assert await store.list_successions() == [succession]
    assert await store.inherited_authors("heir") == ("old",)
    assert await store.inherited_authors("old") == ()
    assert await store.inherited_authors("nobody") == ()


async def test_retire_agent_refusals_change_nothing(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await store.register_agent("waiting")
    with pytest.raises(InvalidInput):
        await store.retire_agent("old", "old", "x")
    with pytest.raises(UnknownAgent):
        await store.retire_agent("ghost", "heir", "x")
    with pytest.raises(UnknownAgent):
        await store.retire_agent("old", "ghost", "x")
    with pytest.raises(InvalidAgentStatus):
        await store.retire_agent("old", "waiting", "x")
    with pytest.raises(InvalidAgentStatus):
        await store.retire_agent("waiting", "heir", "x")
    assert await store.list_successions() == []
    assert (await store.get_agent("old")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    await store.retire_agent("old", "heir", "x")
    with pytest.raises(SuccessionRefused, match="already retired"):
        await store.retire_agent("old", "heir", "x")
    await _active(adapter, "third", red)
    with pytest.raises(InvalidAgentStatus):  # a revoked successor
        await store.retire_agent("third", "old", "x")
    assert [s.retired_agent for s in await store.list_successions()] == ["old"]


async def test_a_revoked_agent_may_be_retired_and_chains_resolve_transitively(
    adapter: Adapter,
) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await _active(adapter, "c", red)
    await store.revoke_agent("old")
    await store.retire_agent("old", "heir", "x")  # already revoked: still hands down
    await store.retire_agent("heir", "c", "x")
    assert await store.inherited_authors("c") == ("heir", "old")
    assert await store.inherited_authors("heir") == ("old",)
    await store.undo_succession("heir")
    assert await store.inherited_authors("c") == ()
    assert await store.inherited_authors("heir") == ("old",)


async def test_a_cycle_is_refused_by_both_adapters(adapter: Adapter) -> None:
    store = adapter.store
    await _people(adapter)
    await store.retire_agent("old", "heir", "x")
    # Re-activate behind the guard (an older pod could): old would then succeed the agent
    # that hands down to it.
    if adapter.kind == "memory":
        agents = store._agents  # type: ignore[attr-defined]
        agents["old"] = replace(agents["old"], status=AgentStatus.ACTIVE)
    else:
        conn = await asyncpg.connect(Settings().database_url)
        try:
            await conn.execute("UPDATE agents SET status = 'active' WHERE name = 'old'")
        finally:
            await conn.close()
    with pytest.raises(SuccessionRefused, match="cycle"):
        await store.retire_agent("heir", "old", "x")
    assert (await store.get_agent("heir")).status is AgentStatus.ACTIVE  # type: ignore[union-attr]
    assert [s.retired_agent for s in await store.list_successions()] == ["old"]


async def test_undo_succession_deletes_it_and_leaves_the_agent_revoked(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await store.retire_agent("old", "heir", "x")
    undone = await store.undo_succession("old")
    assert undone.successor == "heir"
    assert await store.list_successions() == [] and await store.get_succession("old") is None
    assert (await store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    with pytest.raises(UnknownSuccession):
        await store.undo_succession("old")
    await store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id=red)
    await store.retire_agent("old", "heir", "again")  # free to retire once more
    assert (await store.get_succession("old")).by == "again"  # type: ignore[union-attr]


async def test_a_retired_agent_cannot_be_activated_until_undone(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await store.retire_agent("old", "heir", "x")
    with pytest.raises(AgentRetired, match="undo the retirement") as refused:
        await store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id=red)
    assert refused.value.successor == "heir"
    assert (await store.get_agent("old")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    await store.undo_succession("old")
    await store.activate_agent("old", trust_level=TrustLevel.LURKER, home_fleet_id=red)


async def test_list_successions_is_newest_first(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await _active(adapter, "p", red)
    await store.retire_agent("old", "heir", "x")
    await store.retire_agent("p", "heir", "x")
    assert [s.retired_agent for s in await store.list_successions()] == ["p", "old"]


async def test_handed_down_counts_active_notes_fleet_memories_and_pins(adapter: Adapter) -> None:
    store = adapter.store
    red, blue = await _people(adapter)
    await write(adapter, "n1", author="old", scope="self")
    await write(adapter, "n2", author="old", scope="self", hot=1)
    gone = await write(adapter, "gone", author="old", scope="self", hot=2)
    await store.withdraw_entry(gone.id, "x", by_user="old")
    # A fleet write must be in the author's home fleet (ADR 0112): old wrote in
    # blue while blue was its home, then moved to red.
    await store.set_agent_home_fleet("old", blue)
    await write(adapter, "in blue", author="old", scope="fleet", fleet_id=blue, hot=4)
    await store.set_agent_home_fleet("old", red)
    pinned = await write(adapter, "in red", author="old", scope="fleet", fleet_id=red, hot=3)
    await write(adapter, "other's", author="heir", scope="self", hot=5)
    await store.pin_entry(red, pinned.id, "old", 10)
    facts = await store.handed_down(("old",), blue)
    assert (facts.self_notes, facts.fleet_memories, facts.fleet_memories_elsewhere, facts.pins) == (
        2,
        2,
        1,
        1,
    )
    assert facts.authors == ("old",)
    both = await store.handed_down(("old", "heir"), blue)
    assert both.self_notes == 3
    nobody = await store.handed_down(("nobody",), None)
    assert (nobody.self_notes, nobody.fleet_memories, nobody.pins) == (0, 0, 0)


async def test_inherited_authors_read_like_own_in_list_and_search(adapter: Adapter) -> None:
    store = adapter.store
    red, blue = await _people(adapter)
    note = await write(adapter, "old kaniko note", author="old", scope="self")
    elsewhere = await write(
        adapter, "old red kaniko fact", author="old", scope="fleet", fleet_id=red, hot=1
    )
    stranger = await write(adapter, "stranger kaniko note", author="zed", scope="self", hot=2)
    heir = Visibility(TrustLevel.CONTRIBUTOR, "heir", blue, inherited=("old",))
    plain = Visibility(TrustLevel.CONTRIBUTOR, "heir", blue)
    listed = await store.list_entries(EntryFilters(), limit=10, offset=0, visibility=heir)
    assert {e.id for e in listed} == {note.id, elsewhere.id}
    assert await store.list_entries(EntryFilters(), limit=10, offset=0, visibility=plain) == []
    widened = await store.list_entries(
        EntryFilters(authors=("heir", "old")), limit=10, offset=0, visibility=heir
    )
    assert {e.id for e in widened} == {note.id, elsewhere.id}
    assert stranger.id not in {e.id for e in listed}
    hits = await store.search_keyword("kaniko", EntryFilters(), 10, visibility=heir)
    assert set(hits) == {note.id, elsewhere.id}
    assert await store.search_keyword("kaniko", EntryFilters(), 10, visibility=plain) == []


async def test_inherited_authors_filter_the_feedback_log(adapter: Adapter) -> None:
    store = adapter.store
    red, blue = await _people(adapter)
    mine = await write(adapter, "old red", author="old", scope="fleet", fleet_id=red)
    await write(adapter, "zed red", author="zed", scope="fleet", fleet_id=red, hot=1)
    await store.record_feedback(Feedback(mine.id, "peer", "a", Verdict.WRONG, None, T0))
    heir = Visibility(TrustLevel.CONTRIBUTOR, "heir", blue, inherited=("old",))
    items = await store.list_feedback_log(
        FeedbackLogFilters(authors=("heir", "old")), 10, 0, visibility=heir
    )
    assert [i.entry_author for i in items] == ["old"]
    none = await store.list_feedback_log(FeedbackLogFilters(author="heir"), 10, 0, visibility=heir)
    assert none == []


async def test_inherited_pins_count_toward_the_agent_cap(adapter: Adapter) -> None:
    store = adapter.store
    red, _ = await _people(adapter)
    await store.set_agent_home_fleet("heir", red)  # a pin is in the pinner's home fleet (ADR 0112)
    es = [
        await write(adapter, f"p{i}", author="old", scope="fleet", fleet_id=red, hot=i)
        for i in range(4)
    ]
    for e in es[:3]:
        assert isinstance(await store.pin_entry(red, e.id, "old", 10, agent_limit=3), PinWrite)
    refused = await store.pin_entry(
        red, es[3].id, "heir", 10, agent_limit=3, also_owned_by=("old",)
    )
    assert refused is PinRefusal.AGENT
    assert isinstance(await store.pin_entry(red, es[3].id, "heir", 10, agent_limit=3), PinWrite)
