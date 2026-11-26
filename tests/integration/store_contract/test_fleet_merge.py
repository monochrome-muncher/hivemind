"""Fleet merges (ADR 0107) and fleet moves (ADR 0112) on both adapters."""

from __future__ import annotations

import pytest
from tests.integration.store_contract.helpers import (
    NO_SUCH_ID,
    Adapter,
    _agent_in,
    _fleet_entry,
    _two_fleets,
    draft,
    write,
)

from hivemind.domain.access import (
    AgentStatus,
    FleetBusy,
    FleetExists,
    FleetMerged,
    TrustLevel,
    UnknownFleet,
)
from hivemind.domain.entry import Entry
from hivemind.domain.fleet_merge import FleetMergeRefused, FleetMoveRefused, UnknownFleetMerge
from hivemind.domain.pin import PinWrite
from hivemind.domain.validation import InvalidInput


async def _pinned(
    adapter: Adapter, summary: str, fleet_id: str, hot: int = 0, by: str | None = None
) -> str:
    """Pin a new entry, each by its own agent unless ``by`` names one."""
    entry = await _fleet_entry(adapter, summary, fleet_id, hot)
    written = await adapter.store.pin_entry(fleet_id, entry.id, by or f"by-{summary}", limit=99)
    assert isinstance(written, PinWrite)
    return entry.id


async def test_fleet_facts_count_agents_and_entries_by_state(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _fleet_entry(adapter, "live", a)
    gone = await _fleet_entry(adapter, "gone", a)
    await store.withdraw_entry(gone.id, "x", by_user="alice")
    await _fleet_entry(adapter, "theirs", b)
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.LURKER, home_fleet_id=a)
    facts = await store.fleet_facts(a)
    assert (facts.agents, dict(facts.entries)) == (1, {"active": 1, "withdrawn": 1})
    empty = await store.fleet_facts(NO_SUCH_ID)
    assert (empty.agents, dict(empty.entries)) == (0, {})
    assert (await store.fleet_facts("not-a-uuid")).agents == 0


async def test_duplicate_pairs_pair_each_entry_with_its_nearest_in_the_other_fleet(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    mine = await _fleet_entry(adapter, "mine", a, hot=1)
    await _fleet_entry(adapter, "unmatched", a, hot=5)
    twin = await _fleet_entry(adapter, "twin", b, hot=1)
    await _fleet_entry(adapter, "other", b, hot=9)
    gone = await _fleet_entry(adapter, "withdrawn twin", b, hot=5)
    await store.withdraw_entry(gone.id, None, by_user="alice")
    scan = await store.duplicate_pairs(a, b, 0.9, 100)
    assert [(p.entry_id, p.match_id) for p in scan.pairs] == [(mine.id, twin.id)]
    assert scan.pairs[0].similarity == pytest.approx(1.0, abs=1e-4)
    assert (scan.looked_up, scan.truncated) == (2, False)
    capped = await store.duplicate_pairs(a, b, 0.9, 1)
    assert (capped.looked_up, capped.truncated) == (1, True)
    assert (await store.duplicate_pairs(a, NO_SUCH_ID, 0.9, 10)).pairs == ()
    assert (await store.duplicate_pairs("not-a-uuid", b, 0.9, 10)).pairs == ()


async def test_merge_fleets_moves_what_the_design_says(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    live = await _fleet_entry(adapter, "live", a)
    old = await _fleet_entry(adapter, "old", a)
    newer = await store.create_entry(
        draft("newer", scope="fleet", fleet_id=a, supersedes=(old.id,)), adapter.vec(0), "m"
    )
    gone = await _fleet_entry(adapter, "gone", a)
    await store.withdraw_entry(gone.id, None, by_user="alice")
    theirs = await _fleet_entry(adapter, "theirs", b)
    org = await write(adapter, "org entry")
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.PRIVILEGED, home_fleet_id=a)
    pin_a = await _pinned(adapter, "pinned a", a)
    pin_b = await _pinned(adapter, "pinned b", b)
    await store.record_search(a, empty=True)
    await store.record_search(a, empty=False, explore=True)
    await store.record_search(b, empty=False)

    out = await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")

    assert (out.entries_moved, out.agents_moved, out.target_created) == (5, 1, False)
    assert out.target.id == b and out.record.source_fleet_id == a
    assert (out.record.merged_by, out.record.moved_agents) == ("admin:1", ("amy",))
    for e in (live, old, newer, gone, theirs):
        got = await store.get_entry(e.id)
        assert got is not None and got.fleet_id == b
    assert (await store.get_entry(org.id)).fleet_id is None  # type: ignore[union-attr]
    amy = await store.get_agent("amy")
    assert amy is not None and amy.home_fleet_id == b and amy.trust_level is TrustLevel.PRIVILEGED
    assert {p.entry_id for p in await store.list_pins(b)} == {pin_a, pin_b}
    assert await store.list_pins(a) == []
    source = await store.get_fleet(a)
    assert source is not None and source.merged_into == b and source.merged_at is not None
    target = await store.get_fleet(b)
    assert target is not None and target.merged_into is None
    assert {f.id: f.merged_into for f in await store.list_fleets()} == {a: b, b: None}
    counts = {c.fleet_id: (c.searches, c.empty, c.explore) for c in await store.search_counts()}
    assert counts == {b: (3, 1, 1)}
    assert await store.get_fleet_merge(out.record.id) == out.record
    assert [m.id for m in await store.list_fleet_merges(10)] == [out.record.id]


async def test_merge_into_a_new_fleet(adapter: Adapter) -> None:
    store = adapter.store
    a = (await store.create_fleet("a")).id
    e = await _fleet_entry(adapter, "e", a)
    out = await store.merge_fleets(a, new_fleet_name="c", merged_by="admin:1")
    assert out.target_created and out.target.name == "c" and out.entries_moved == 1
    got = await store.get_entry(e.id)
    assert got is not None and got.fleet_id == out.target.id
    assert {f.name for f in await store.list_fleets()} == {"a", "c"}
    with pytest.raises(FleetExists):
        await store.merge_fleets(out.target.id, new_fleet_name="a", merged_by="admin:1")
    assert (await store.get_fleet(out.target.id)).merged_into is None  # type: ignore[union-attr]


async def test_merge_refusals_change_nothing(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    c = (await store.create_fleet("c")).id
    e = await _fleet_entry(adapter, "e", a)
    with pytest.raises(FleetMergeRefused):
        await store.merge_fleets(a, target_fleet_id=a, merged_by="x")
    for source, target in ((NO_SUCH_ID, a), (a, NO_SUCH_ID), ("not-a-uuid", a), (a, "not-a-uuid")):
        with pytest.raises(UnknownFleet):
            await store.merge_fleets(source, target_fleet_id=target, merged_by="x")
    with pytest.raises(InvalidInput):
        await store.merge_fleets(a, merged_by="x")
    with pytest.raises(InvalidInput):
        await store.merge_fleets(a, target_fleet_id=b, new_fleet_name="n", merged_by="x")
    pins = [await _pinned(adapter, f"p{i}", a) for i in range(11)]
    with pytest.raises(InvalidInput):  # over the cap with no choice
        await store.merge_fleets(a, target_fleet_id=b, merged_by="x")
    with pytest.raises(InvalidInput):
        await store.merge_fleets(a, target_fleet_id=b, keep_pin_ids=pins, merged_by="x")
    with pytest.raises(InvalidInput):
        await store.merge_fleets(a, target_fleet_id=b, keep_pin_ids=[NO_SUCH_ID], merged_by="x")
    got = await store.get_entry(e.id)
    assert got is not None and got.fleet_id == a
    assert len(await store.list_pins(a)) == 11
    assert await store.list_fleet_merges(10) == []
    await store.merge_fleets(a, target_fleet_id=b, keep_pin_ids=pins[1:], merged_by="x")
    with pytest.raises(FleetMerged) as gone:
        await store.merge_fleets(a, target_fleet_id=c, merged_by="x")
    assert (gone.value.target_id, gone.value.target_name) == (b, "b")
    with pytest.raises(FleetMerged):
        await store.merge_fleets(c, target_fleet_id=a, merged_by="x")


async def test_a_merged_fleet_refuses_entries_pins_and_members(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    e = await _fleet_entry(adapter, "e", a)
    await store.register_agent("amy")
    await store.register_agent("bob")
    await store.activate_agent("bob", trust_level=TrustLevel.LURKER, home_fleet_id=b)
    await store.merge_fleets(a, target_fleet_id=b, merged_by="x")
    with pytest.raises(FleetMerged, match="'b'"):
        await store.create_entry(
            draft("late", scope="fleet", fleet_id=a), adapter.vec(0), "test-model"
        )
    with pytest.raises(FleetMerged):
        await store.pin_entry(a, e.id, "lead", limit=10)
    with pytest.raises(FleetMerged):
        await store.activate_agent("amy", trust_level=TrustLevel.LURKER, home_fleet_id=a)
    with pytest.raises(FleetMerged):
        await store.set_agent_home_fleet("bob", a)
    assert (await store.get_agent("amy")).status is AgentStatus.PENDING  # type: ignore[union-attr]
    assert (await store.get_agent("bob")).home_fleet_id == b  # type: ignore[union-attr]
    # Org entries, and the target, are unaffected.
    await write(adapter, "org")
    await write(adapter, "ok", scope="fleet", fleet_id=b)


async def test_unmerge_restores_what_moved_and_leaves_what_came_after(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    mine = await _fleet_entry(adapter, "mine", a)
    theirs = await _fleet_entry(adapter, "theirs", b)
    await store.register_agent("amy")
    await store.activate_agent("amy", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=a)
    await store.register_agent("ann")
    await store.activate_agent("ann", trust_level=TrustLevel.LURKER, home_fleet_id=a)
    pin_a = await _pinned(adapter, "pa", a)
    pins_b = [await _pinned(adapter, f"pb{i}", b) for i in range(10)]
    out = await store.merge_fleets(
        a, target_fleet_id=b, keep_pin_ids=[pin_a, *pins_b[1:]], merged_by="x"
    )
    assert [p.entry_id for p in out.pins_dropped] == [pins_b[0]]
    late = await _fleet_entry(adapter, "late", b)
    other = (await store.create_fleet("c")).id
    await store.set_agent_home_fleet("ann", other)

    undone = await store.unmerge_fleet(out.record.id)

    assert undone.entries_restored == 2  # mine and pa's entry
    for e, home in ((mine.id, a), (pin_a, a), (theirs.id, b), (late.id, b)):
        got = await store.get_entry(e)
        assert got is not None and got.fleet_id == home
    assert undone.agents_restored == ("amy",)
    assert (await store.get_agent("amy")).home_fleet_id == a  # type: ignore[union-attr]
    assert (await store.get_agent("ann")).home_fleet_id == other  # type: ignore[union-attr]
    assert undone.pins_returned == 1
    assert [p.entry_id for p in await store.list_pins(a)] == [pin_a]
    assert {p.entry_id for p in await store.list_pins(b)} == set(pins_b)
    assert [p.entry_id for p in undone.pins_restored] == [pins_b[0]]
    assert undone.pins_skipped == () and undone.record.undone_at is not None
    source = await store.get_fleet(a)
    assert source is not None and source.merged_into is None and source.merged_at is None
    stored = await store.get_fleet_merge(out.record.id)
    assert stored is not None and stored.undone_at is not None
    # The source takes members and entries again.
    await store.set_agent_home_fleet("ann", a)
    await _fleet_entry(adapter, "again", a)


async def test_unmerge_refusals(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    c = (await store.create_fleet("c")).id
    first = await store.merge_fleets(a, target_fleet_id=c, merged_by="x")
    second = await store.merge_fleets(b, target_fleet_id=c, merged_by="x")
    assert [m.id for m in await store.list_fleet_merges(10)] == [second.record.id, first.record.id]
    with pytest.raises(FleetMergeRefused, match="newer"):
        await store.unmerge_fleet(first.record.id)
    await store.unmerge_fleet(second.record.id)
    with pytest.raises(FleetMergeRefused, match="already undone"):
        await store.unmerge_fleet(second.record.id)
    chained = await store.merge_fleets(c, target_fleet_id=b, merged_by="x")  # c -> b
    with pytest.raises(FleetMergeRefused):
        await store.unmerge_fleet(first.record.id)  # its target c is merged now
    await store.unmerge_fleet(chained.record.id)
    await store.unmerge_fleet(first.record.id)
    for bad in (NO_SUCH_ID, "not-a-uuid"):
        with pytest.raises(UnknownFleetMerge):
            await store.unmerge_fleet(bad)
    assert await store.get_fleet_merge(NO_SUCH_ID) is None
    assert await store.get_fleet_merge("not-a-uuid") is None


async def test_unmerge_skips_the_oldest_dropped_pins_that_do_not_fit(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    mine = [await _pinned(adapter, f"a{i}", a) for i in range(2)]
    theirs = [await _pinned(adapter, f"b{i}", b) for i in range(10)]
    out = await store.merge_fleets(
        a, target_fleet_id=b, keep_pin_ids=[*mine, *theirs[2:]], merged_by="x"
    )
    assert {p.entry_id for p in out.pins_dropped} == set(theirs[:2])
    for entry_id in mine:
        assert await store.unpin_entry(b, entry_id)
    fresh = await _pinned(adapter, "fresh", b)
    undone = await store.unmerge_fleet(out.record.id)
    assert [p.entry_id for p in undone.pins_restored] == [theirs[1]]
    assert [p.entry_id for p in undone.pins_skipped] == [theirs[0]]
    assert {p.entry_id for p in await store.list_pins(b)} == {*theirs[1:], fresh}


async def test_merge_is_refused_when_the_pins_changed_since_the_preview(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    seen = [await _pinned(adapter, "seen", a)]
    late = await _pinned(adapter, "late", b)  # pinned after the preview was taken
    with pytest.raises(FleetMergeRefused, match="preview again"):
        await store.merge_fleets(a, target_fleet_id=b, expected_pin_ids=seen, merged_by="x")
    with pytest.raises(FleetMergeRefused, match="preview again"):
        await store.merge_fleets(
            a, target_fleet_id=b, expected_pin_ids=[*seen, late, NO_SUCH_ID], merged_by="x"
        )
    assert await store.list_fleet_merges(10) == []
    assert (await store.get_fleet(a)).merged_into is None  # type: ignore[union-attr]
    out = await store.merge_fleets(
        a, target_fleet_id=b, expected_pin_ids=[late, *seen], merged_by="x"
    )
    assert (len(out.pins_kept), out.pins_dropped) == (2, ())


async def test_merge_reads_ids_the_same_on_both_adapters(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    with pytest.raises(UnknownFleet):  # an unknown source wins over a taken new name
        await store.merge_fleets(NO_SUCH_ID, new_fleet_name="a", merged_by="x")
    with pytest.raises(FleetExists):
        await store.merge_fleets(a, new_fleet_name="b", merged_by="x")
    out = await store.merge_fleets(a.upper(), target_fleet_id=b.upper(), merged_by="x")
    assert (out.record.source_fleet_id, out.record.target_fleet_id) == (a, b)
    with pytest.raises(UnknownFleetMerge):
        await store.unmerge_fleet("not-a-uuid")
    undone = await store.unmerge_fleet(out.record.id.upper())
    assert undone.record.id == out.record.id


async def test_unmerge_keeps_to_three_restored_pins_per_agent(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    pins = [await _pinned(adapter, f"p{i}", a, by="lead") for i in range(4)]
    out = await store.merge_fleets(a, target_fleet_id=b, keep_pin_ids=[pins[3]], merged_by="x")
    assert {p.entry_id for p in out.pins_dropped} == set(pins[:3])
    undone = await store.unmerge_fleet(out.record.id)
    # pins[3] came back with its entry; "lead" may hold two more, so the oldest is skipped.
    assert [p.entry_id for p in undone.pins_restored] == [pins[2], pins[1]]
    assert [p.entry_id for p in undone.pins_skipped] == [pins[0]]
    assert {p.entry_id for p in await store.list_pins(a)} == set(pins[1:])


async def _authored(
    adapter: Adapter, summary: str, fleet_id: str, author: str, supersedes: tuple[str, ...] = ()
) -> Entry:
    return await write(
        adapter, summary, scope="fleet", fleet_id=fleet_id, author=author, supersedes=supersedes
    )


async def test_move_agents_moves_the_agents_and_their_fleet_memories(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    for name in ("amy", "ann", "bob"):
        await _agent_in(adapter, name, a)
    live = await _authored(adapter, "live", a, "amy")
    gone = await _authored(adapter, "gone", a, "ann")
    await store.withdraw_entry(gone.id, "x", by_user="alice")
    bobs = await _authored(adapter, "bobs", a, "bob")
    own = await write(adapter, "self note", author="amy", scope="self")
    org = await write(adapter, "org note", author="amy", scope="org")
    theirs = await _authored(adapter, "theirs", b, "bea")
    await store.record_search(a, empty=True)

    plan = await store.plan_fleet_move(a, ["amy", "ann", "amy"])
    assert (set(plan.entry_ids), plan.held_back) == ({live.id, gone.id}, 0)
    assert dict(plan.states or {}) == {"active": 1, "withdrawn": 1}

    out = await store.move_agents(a, ["amy", "ann"], target_fleet_id=b, moved_by="admin:1")

    assert (out.entries_moved, out.entries_held_back, out.agents_moved) == (2, 0, 2)
    assert out.record.kind == "move" and out.record.moved_agents == ("amy", "ann")
    assert (out.record.source_fleet_id, out.record.target_fleet_id) == (a, b)
    assert out.record.merged_by == "admin:1" and not out.target_created
    found = await store.get_entries([live.id, gone.id, bobs.id, own.id, org.id, theirs.id])
    fleets = {e.id: e.fleet_id for e in found.values()}
    assert fleets == {
        live.id: b,
        gone.id: b,
        bobs.id: a,
        own.id: None,
        org.id: None,
        theirs.id: b,
    }
    homes = {n: (await store.get_agent(n)).home_fleet_id for n in ("amy", "ann", "bob")}  # type: ignore[union-attr]
    assert homes == {"amy": b, "ann": b, "bob": a}
    assert {f.id: f.merged_into for f in await store.list_fleets()} == {a: None, b: None}
    assert {c.fleet_id: c.searches for c in await store.search_counts()} == {a: 1}
    assert await store.get_fleet_merge(out.record.id) == out.record
    assert [m.kind for m in await store.list_fleet_merges(10)] == ["move"]


async def test_move_agents_into_a_new_fleet(adapter: Adapter) -> None:
    store = adapter.store
    a, _ = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    mine = await _authored(adapter, "mine", a, "amy")
    out = await store.move_agents(a, ["amy"], new_fleet_name="c", moved_by="admin:1")
    assert out.target_created and out.target.name == "c" and out.entries_moved == 1
    assert (await store.get_entry(mine.id)).fleet_id == out.target.id  # type: ignore[union-attr]
    with pytest.raises(FleetExists):
        await store.move_agents(a, ["amy"], new_fleet_name="a", moved_by="x")
    await store.move_agents(out.target.id, ["amy"], target_fleet_id=a, moved_by="x")


async def test_move_follows_the_chain_rule(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    for name in ("amy", "bob"):
        await _agent_in(adapter, name, a)
    v1 = await _authored(adapter, "v1", a, "amy")
    v2 = await _authored(adapter, "v2", a, "bob", (v1.id,))  # a stayer's head: v1 stays
    c1 = await _authored(adapter, "c1", a, "amy")
    c2 = await _authored(adapter, "c2", a, "bob", (c1.id,))
    c3 = await _authored(adapter, "c3", a, "amy", (c2.id,))  # amy's head: c1 moves, c2 stays
    d1 = await _authored(adapter, "d1", a, "amy")
    d2 = await _authored(adapter, "d2", a, "amy", (d1.id,))
    plan = await store.plan_fleet_move(a, ["amy"])
    assert (set(plan.entry_ids), plan.held_back) == ({c1.id, c3.id, d1.id, d2.id}, 1)
    out = await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    assert (out.entries_moved, out.entries_held_back) == (4, 1)
    now = {
        e.id: e.fleet_id
        for e in (await store.get_entries([x.id for x in (v1, v2, c1, c2, c3, d1, d2)])).values()
    }
    assert now == {v1.id: a, v2.id: a, c1.id: b, c2.id: a, c3.id: b, d1.id: b, d2.id: b}


async def test_move_pins_follow_and_the_picker_applies(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    mine = []
    for i in range(6):
        e = await _authored(adapter, f"m{i}", a, "amy")
        assert isinstance(await store.pin_entry(a, e.id, f"p{i}", limit=99), PinWrite)
        mine.append(e.id)
    stays = await _pinned(adapter, "stays", a)  # an author who is not moving
    theirs = [await _pinned(adapter, f"t{i}", b) for i in range(6)]
    with pytest.raises(InvalidInput):
        await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")  # 12 > 10
    shown = [*mine, *theirs]
    with pytest.raises(FleetMoveRefused, match="preview again"):
        await store.move_agents(
            a, ["amy"], target_fleet_id=b, expected_pin_ids=shown[1:], moved_by="x"
        )
    assert await store.list_fleet_merges(10) == []
    keep = [*mine[1:], *theirs[1:]]
    out = await store.move_agents(
        a, ["amy"], target_fleet_id=b, keep_pin_ids=keep, expected_pin_ids=shown, moved_by="x"
    )
    assert {p.entry_id for p in out.pins_dropped} == {mine[0], theirs[0]}
    assert {p.entry_id for p in await store.list_pins(b)} == set(keep)
    assert {p.entry_id for p in await store.list_pins(a)} == {stays}

    undone = await store.unmerge_fleet(out.record.id)
    assert undone.record.kind == "move" and undone.agents_restored == ("amy",)
    assert undone.pins_returned == 5
    assert {p.entry_id for p in await store.list_pins(a)} == {stays, *mine[1:], mine[0]}
    assert {p.entry_id for p in await store.list_pins(b)} == set(theirs)


async def test_move_refusals_change_nothing(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    c = (await store.create_fleet("c")).id
    await _agent_in(adapter, "amy", a)
    await _agent_in(adapter, "cy", c)
    mine = await _authored(adapter, "mine", a, "amy")
    with pytest.raises(FleetMoveRefused, match="cy, ghost"):
        await store.move_agents(a, ["amy", "ghost", "cy"], target_fleet_id=b, moved_by="x")
    with pytest.raises(FleetMoveRefused):
        await store.move_agents(a, ["amy"], target_fleet_id=a, moved_by="x")
    with pytest.raises(InvalidInput):
        await store.move_agents(a, [], target_fleet_id=b, moved_by="x")
    with pytest.raises(InvalidInput):
        await store.move_agents(a, ["amy"], moved_by="x")
    with pytest.raises(InvalidInput):
        await store.move_agents(a, ["amy"], target_fleet_id=b, new_fleet_name="n", moved_by="x")
    with pytest.raises(UnknownFleet):
        await store.move_agents(NO_SUCH_ID, ["amy"], target_fleet_id=b, moved_by="x")
    with pytest.raises(UnknownFleet):
        await store.move_agents(a, ["amy"], target_fleet_id=NO_SUCH_ID, moved_by="x")
    with pytest.raises(UnknownFleet):
        await store.move_agents("not-a-uuid", ["amy"], target_fleet_id=b, moved_by="x")
    with pytest.raises(UnknownFleet):  # an unknown source wins over a taken new name
        await store.move_agents(NO_SUCH_ID, ["amy"], new_fleet_name="a", moved_by="x")
    with pytest.raises(FleetExists):
        await store.move_agents(a, ["amy"], new_fleet_name="b", moved_by="x")
    await store.merge_fleets(c, target_fleet_id=b, merged_by="x")
    with pytest.raises(FleetMerged):
        await store.move_agents(a, ["amy"], target_fleet_id=c, moved_by="x")
    with pytest.raises(FleetMerged):
        await store.move_agents(c, ["cy"], target_fleet_id=a, moved_by="x")
    assert (await store.get_agent("amy")).home_fleet_id == a  # type: ignore[union-attr]
    assert (await store.get_entry(mine.id)).fleet_id == a  # type: ignore[union-attr]
    assert [m.kind for m in await store.list_fleet_merges(10)] == ["merge"]
    assert {f.name for f in await store.list_fleets()} == {"a", "b", "c"}  # no stray new fleet


async def test_move_ids_are_read_the_same_on_both_adapters(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    out = await store.move_agents(a.upper(), ["amy"], target_fleet_id=b.upper(), moved_by="x")
    assert (out.record.source_fleet_id, out.record.target_fleet_id) == (a, b)
    assert (await store.plan_fleet_move("not-a-uuid", ["amy"])).entry_ids == ()


async def test_a_write_by_a_moved_author_for_the_old_fleet_is_refused_as_busy(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    with pytest.raises(FleetBusy, match="home fleet of amy changed"):
        await _authored(adapter, "late", a, "amy")
    assert (await _authored(adapter, "now", b, "amy")).fleet_id == b


async def test_undo_of_a_move_follows_the_newest_standing_rule(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    c = (await store.create_fleet("c")).id
    for name in ("amy", "ann"):
        await _agent_in(adapter, name, a)
    mine = await _authored(adapter, "mine", a, "amy")
    first = await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    second = await store.move_agents(a, ["ann"], target_fleet_id=c, moved_by="x")  # same source
    with pytest.raises(FleetMoveRefused, match="newer"):
        await store.unmerge_fleet(first.record.id)
    await store.unmerge_fleet(second.record.id)
    merged = await store.merge_fleets(b, target_fleet_id=c, merged_by="x")  # the target merged away
    with pytest.raises(FleetMoveRefused):
        await store.unmerge_fleet(first.record.id)
    await store.unmerge_fleet(merged.record.id)
    undone = await store.unmerge_fleet(first.record.id)
    assert (undone.entries_restored, undone.agents_restored) == (1, ("amy",))
    assert (await store.get_entry(mine.id)).fleet_id == a  # type: ignore[union-attr]
    source = await store.get_fleet(a)
    assert source is not None and source.merged_into is None
    with pytest.raises(FleetMoveRefused, match="already undone"):
        await store.unmerge_fleet(first.record.id)
    # A merge refusal stays a plain merge refusal.
    older = await store.merge_fleets(c, new_fleet_name="d", merged_by="x")
    await store.move_agents(a, ["amy"], target_fleet_id=older.target.id, moved_by="x")
    with pytest.raises(FleetMergeRefused, match="newer") as caught:
        await store.unmerge_fleet(older.record.id)
    assert not isinstance(caught.value, FleetMoveRefused)


async def test_undo_of_a_move_applies_the_source_pin_cap_to_returning_pins(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    mine = []
    for i in range(4):
        e = await _authored(adapter, f"m{i}", a, "amy")
        assert isinstance(await store.pin_entry(a, e.id, f"p{i}", limit=99), PinWrite)
        mine.append(e.id)
    out = await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    for i in range(8):
        await _pinned(adapter, f"s{i}", a)
    undone = await store.unmerge_fleet(out.record.id)
    assert undone.pins_returned == 2
    assert {p.entry_id for p in undone.pins_unpinned} == set(mine[:2])
    assert {p.entry_id for p in undone.pins_skipped} == set(mine[:2])
    assert len(await store.list_pins(a)) == 10
    assert await store.list_pins(b) == []
    assert (await store.get_entry(mine[0])).fleet_id == a  # type: ignore[union-attr]


async def test_the_home_fleet_guard_ignores_unregistered_authors_and_private_notes(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await _agent_in(adapter, "amy", a)
    await store.move_agents(a, ["amy"], target_fleet_id=b, moved_by="x")
    assert (await _authored(adapter, "unregistered", a, "ghost")).fleet_id == a
    note = await write(adapter, "private", author="amy", scope="self")
    assert note.fleet_id is None
    stale_pin = await _authored(adapter, "to pin", a, "ghost")
    with pytest.raises(FleetBusy):  # a pin by the moved agent for the old fleet is refused
        await store.pin_entry(a, stale_pin.id, "amy", limit=10)
    assert isinstance(await store.pin_entry(a, stale_pin.id, "ghost", limit=10), PinWrite)
