"""Move to self (ADR 0145) on both adapters."""

from __future__ import annotations

import uuid

import pytest
from tests.integration.store_contract.helpers import (
    Adapter,
    _active,
    _clean,
    _people,
    _state,
    _vis,
    draft,
    write,
)

from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryFilters, EntryState
from hivemind.domain.self_move import RemovedPin, SelfMoveRefused
from hivemind.ports import SupersedeConflict, UnknownEntry


async def test_move_to_self_takes_the_authors_earlier_versions_and_their_pins(
    adapter: Adapter,
) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    first = await write(adapter, "v1", scope="fleet", fleet_id=blue)
    head = await store.create_entry(
        draft("v2", scope="fleet", fleet_id=blue, supersedes=(first.id,)), adapter.vec(0), "m"
    )
    stays = await write(adapter, "someone else's", scope="fleet", fleet_id=blue, author="bob")
    await store.pin_entry(blue, first.id, "boss", limit=10)
    await store.pin_entry(blue, stays.id, "boss", limit=10)

    move = await store.move_entry_to_self(head.id)

    assert move.entry.id == head.id and move.entry.scope == "self"
    assert move.entry.state is EntryState.ACTIVE and move.entry.fleet_id == blue
    assert move.earlier_versions == (first.id,)
    assert move.unpinned == (RemovedPin(blue, first.id),)
    moved_first = await store.get_entry(first.id)
    assert moved_first is not None and moved_first.scope == "self"
    assert moved_first.state is EntryState.SUPERSEDED and moved_first.superseded_by == head.id
    assert [p.entry_id for p in await store.list_pins(blue)] == [stays.id]
    fleet_reader = _vis(TrustLevel.CONTRIBUTOR, "carol", blue)
    visible = await store.list_entries(EntryFilters(include_inactive=True), visibility=fleet_reader)
    assert [e.id for e in visible] == [stays.id]


async def test_move_to_self_refuses_what_is_not_an_active_fleet_memory(adapter: Adapter) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    private = await write(adapter, "mine", scope="self")
    gone = await write(adapter, "gone", scope="fleet", fleet_id=blue)
    await store.withdraw_entry(gone.id, None, by_user="alice")
    legacy = await write(adapter, "legacy")
    for entry_id in (private.id, gone.id, legacy.id):
        with pytest.raises(SelfMoveRefused):
            await store.move_entry_to_self(entry_id)
    with pytest.raises(UnknownEntry):
        await store.move_entry_to_self(str(uuid.uuid4()))
    with pytest.raises(UnknownEntry):
        await store.move_entry_to_self("not-a-uuid")


async def test_move_to_self_refuses_when_a_version_replaced_another_agents_memory(
    adapter: Adapter,
) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    bobs = await write(adapter, "bob v1", scope="fleet", fleet_id=blue, author="bob")
    mine = await store.create_entry(
        draft("alice v2", scope="fleet", fleet_id=blue, supersedes=(bobs.id,)), adapter.vec(0), "m"
    )
    head = await store.create_entry(
        draft("alice v3", scope="fleet", fleet_id=blue, supersedes=(mine.id,)), adapter.vec(0), "m"
    )
    with pytest.raises(SelfMoveRefused, match=bobs.id):
        await store.move_entry_to_self(head.id)
    for entry_id in (bobs.id, mine.id, head.id):
        unchanged = await store.get_entry(entry_id)
        assert unchanged is not None and unchanged.scope == "fleet"


async def _old_and_heir_in_one_fleet(a: Adapter) -> str:
    red = (await a.store.create_fleet("red")).id
    await _active(a, "old", red)
    await _active(a, "heir", red)
    return red


async def test_move_to_self_takes_the_versions_an_heir_inherited(adapter: Adapter) -> None:
    store = adapter.store
    red = await _old_and_heir_in_one_fleet(adapter)
    await _active(adapter, "third", red)
    v1 = await write(adapter, "old v1", scope="fleet", fleet_id=red, author="old")
    await store.pin_entry(red, v1.id, "boss", limit=10)
    await store.retire_agent("old", "heir", "admin:1")
    v2 = await store.create_entry(
        draft("heir v2", scope="fleet", fleet_id=red, author="heir", supersedes=(v1.id,)),
        adapter.vec(0),
        "m",
    )
    await store.retire_agent("heir", "third", "admin:1")
    head = await store.create_entry(
        draft("third v3", scope="fleet", fleet_id=red, author="third", supersedes=(v2.id,)),
        adapter.vec(0),
        "m",
    )

    move = await store.move_entry_to_self(head.id)

    assert move.earlier_versions == tuple(sorted((v1.id, v2.id)))
    assert move.unpinned == (RemovedPin(red, v1.id),)
    for entry_id in (v1.id, v2.id, head.id):
        moved = await store.get_entry(entry_id)
        assert moved is not None and moved.scope == "self"


async def test_move_to_self_still_refuses_a_predecessors_line_that_replaced_someone_else(
    adapter: Adapter,
) -> None:
    store = adapter.store
    red = await _old_and_heir_in_one_fleet(adapter)
    bobs = await write(adapter, "bob v1", scope="fleet", fleet_id=red, author="bob")
    olds = await store.create_entry(
        draft("old v2", scope="fleet", fleet_id=red, author="old", supersedes=(bobs.id,)),
        adapter.vec(0),
        "m",
    )
    await store.retire_agent("old", "heir", "admin:1")
    head = await store.create_entry(
        draft("heir v3", scope="fleet", fleet_id=red, author="heir", supersedes=(olds.id,)),
        adapter.vec(0),
        "m",
    )
    with pytest.raises(SelfMoveRefused, match=bobs.id):
        await store.move_entry_to_self(head.id)
    for entry_id in (bobs.id, olds.id, head.id):
        unchanged = await store.get_entry(entry_id)
        assert unchanged is not None and unchanged.scope == "fleet"


async def test_a_private_memory_is_superseded_only_by_its_owner_or_an_org_successor(
    adapter: Adapter,
) -> None:
    store = adapter.store
    red, blue = await _people(adapter)
    note = await write(adapter, "old's note", scope="self", author="old")
    with pytest.raises(SupersedeConflict):
        await store.create_entry(
            draft("bob's", scope="fleet", fleet_id=red, author="bob", supersedes=(note.id,)),
            adapter.vec(0),
            "m",
        )
    await store.retire_agent("old", "heir", "admin:1")
    by_heir = await store.create_entry(
        draft("heir's", scope="fleet", fleet_id=blue, author="heir", supersedes=(note.id,)),
        adapter.vec(0),
        "m",
    )
    flipped = await store.get_entry(note.id)
    assert flipped is not None and flipped.superseded_by == by_heir.id
    other = await write(adapter, "other note", scope="self", author="bob")
    await store.create_entry(
        draft("org", scope="org", author="admin", supersedes=(other.id,)), adapter.vec(0), "m"
    )


async def test_a_cleanup_undo_never_replaces_a_memory_moved_to_self_since(
    adapter: Adapter,
) -> None:
    store = adapter.store
    blue = (await store.create_fleet("blue")).id
    good = await write(adapter, "alice's fleet memory", scope="fleet", fleet_id=blue)
    junk = await store.create_entry(
        draft("bad over it", author="bad", scope="fleet", fleet_id=blue, supersedes=(good.id,)),
        adapter.vec(0),
        "m",
    )
    await store.pin_entry(blue, good.id, "alice", limit=10)
    out = await _clean(adapter, restore_ids=[good.id])
    assert out.record is not None
    await store.pin_entry(blue, good.id, "alice", limit=10)
    moved = await store.move_entry_to_self(good.id)
    assert moved.unpinned == (RemovedPin(blue, good.id),)

    undone = await store.undo_agent_cleanup(out.record.id)

    assert (undone.resuperseded, undone.resupersede_skipped) == (0, 1)
    kept = await _state(adapter, good.id)
    assert (kept.state, kept.scope, kept.superseded_by) == (EntryState.ACTIVE, "self", None)
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE
    assert await store.list_pins(blue) == []
