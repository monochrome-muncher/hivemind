"""Restoring one withdrawn memory (ADR 0124) on both adapters."""

from __future__ import annotations

from dataclasses import replace

import pytest
from tests.integration.store_contract.helpers import (
    NO_SUCH_ID,
    Adapter,
    _clean,
    _junk,
    _state,
    vector_ids,
    write,
)

from hivemind.domain.agent_cleanup import cleanup_reason
from hivemind.domain.entry import Entry, EntryFilters, EntryState
from hivemind.domain.entry_restore import EntryNotWithdrawn, RestoreChainConflict, RestoreViaCleanup
from hivemind.ports import SupersedeConflict, UnknownEntry


async def test_restore_puts_a_withdrawn_memory_back_and_search_finds_it(
    adapter: Adapter,
) -> None:
    store = adapter.store
    entry = await write(adapter, "kaniko cache is on", hot=4)
    other = await write(adapter, "unrelated thing", hot=9)
    await store.withdraw_entry(entry.id, "mistaken", by_user="alice")
    gone = await _state(adapter, entry.id)
    assert gone.state is EntryState.WITHDRAWN and gone.withdrawn_at is not None
    assert await store.search_keyword("kaniko", EntryFilters(), 10) == []
    assert await vector_ids(store, adapter.vec(4), EntryFilters(), 10) == [other.id]

    restoration = await store.restore_entry(entry.id)

    assert restoration.previous_reason == "mistaken"
    back = restoration.entry
    assert back.state is EntryState.ACTIVE
    assert back.withdrawn_reason is None and back.withdrawn_at is None
    assert back.superseded_by is None
    assert replace(await _state(adapter, entry.id), embedding=None) == replace(back, embedding=None)
    # The embedding and the text index survived the withdrawal.
    assert await store.search_keyword("kaniko", EntryFilters(), 10) == [entry.id]
    assert (await vector_ids(store, adapter.vec(4), EntryFilters(), 10))[0] == entry.id


async def test_restore_refuses_what_is_not_withdrawn_and_what_is_unknown(
    adapter: Adapter,
) -> None:
    store = adapter.store
    active = await write(adapter, "active one")
    old = await write(adapter, "old")
    await write(adapter, "new", supersedes=(old.id,))
    with pytest.raises(EntryNotWithdrawn):
        await store.restore_entry(active.id)
    with pytest.raises(EntryNotWithdrawn):
        await store.restore_entry(old.id)
    for bad in (NO_SUCH_ID, "not-a-uuid"):
        with pytest.raises(UnknownEntry):
            await store.restore_entry(bad)
    assert (await _state(adapter, active.id)).state is EntryState.ACTIVE
    assert (await _state(adapter, old.id)).state is EntryState.SUPERSEDED


async def test_restore_is_not_repeatable_and_a_second_withdrawal_works(
    adapter: Adapter,
) -> None:
    store = adapter.store
    entry = await write(adapter, "flip")
    await store.withdraw_entry(entry.id, "first", by_user="alice")
    await store.restore_entry(entry.id)
    with pytest.raises(EntryNotWithdrawn):
        await store.restore_entry(entry.id)
    await store.withdraw_entry(entry.id, "second", by_user="alice")
    assert (await store.restore_entry(entry.id)).previous_reason == "second"


async def test_restoring_a_withdrawn_head_keeps_the_chain_below_it(adapter: Adapter) -> None:
    """P is superseded by H, H is withdrawn (P stays superseded, nothing is
    current). Restoring H makes it the one head again."""
    store = adapter.store
    p = await write(adapter, "v1")
    h = await write(adapter, "v2", supersedes=(p.id,))
    await store.withdraw_entry(h.id, "wrong", by_user="alice")
    with pytest.raises(SupersedeConflict):  # a withdrawn memory cannot be superseded
        await write(adapter, "v3", supersedes=(h.id,))

    await store.restore_entry(h.id)

    assert (await _state(adapter, h.id)).state is EntryState.ACTIVE
    below = await _state(adapter, p.id)
    assert below.state is EntryState.SUPERSEDED and below.superseded_by == h.id
    await write(adapter, "v3", supersedes=(h.id,))  # and it is supersedable again


async def test_restore_refuses_a_memory_a_standing_cleanup_withdrew(adapter: Adapter) -> None:
    store = adapter.store
    junk = await _junk(adapter, "junk")
    out = await _clean(adapter)
    assert out.record is not None
    with pytest.raises(RestoreViaCleanup) as exc:
        await store.restore_entry(junk.id)
    assert out.record.id in str(exc.value)
    assert (await _state(adapter, junk.id)).state is EntryState.WITHDRAWN

    await store.undo_agent_cleanup(out.record.id)  # the undo is the way back
    with pytest.raises(EntryNotWithdrawn):
        await store.restore_entry(junk.id)


async def test_restore_of_a_hand_typed_cleanup_reason_is_allowed(adapter: Adapter) -> None:
    """Only a cleanup record that lists the memory makes it a cleanup's."""
    entry = await write(adapter, "typed")
    await adapter.store.withdraw_entry(entry.id, cleanup_reason(NO_SUCH_ID), by_user="alice")
    assert (await adapter.store.restore_entry(entry.id)).entry.state is EntryState.ACTIVE


async def _cleaned_chain(adapter: Adapter) -> tuple[Entry, Entry, str]:
    """`good` was superseded by the bad agent's `junk`; a cleanup withdrew
    `junk` and put `good` back (the link between them is gone)."""
    good = await write(adapter, "good", author="good")
    junk = await _junk(adapter, "junk", supersedes=(good.id,))
    out = await _clean(adapter, restore_ids=[good.id])
    assert out.record is not None and out.restored == 1
    return good, junk, out.record.id


async def _hand_withdrawn_and_cleanup_undone(adapter: Adapter) -> tuple[Entry, Entry]:
    """good was superseded by junk; cleanup C withdrew junk and put good back;
    good was withdrawn by hand; C was undone. junk is active, good withdrawn."""
    store = adapter.store
    good, junk, cleanup_id = await _cleaned_chain(adapter)
    await store.withdraw_entry(good.id, "by hand", by_user="alice")
    undone = await store.undo_agent_cleanup(cleanup_id)
    assert (undone.reactivated, undone.resupersede_skipped) == (1, 1)
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE
    return good, junk


async def test_restore_refuses_a_memory_a_cleanup_once_put_back(adapter: Adapter) -> None:
    """Whatever the successor's state now, the cut link means a restore could
    leave two heads once the successor comes back, so the answer is: write a
    new memory. This also holds while the cleanup still stands."""
    store = adapter.store
    good, junk, cleanup_id = await _cleaned_chain(adapter)
    await store.withdraw_entry(good.id, "by hand", by_user="alice")
    with pytest.raises(RestoreChainConflict):
        await store.restore_entry(good.id)
    undone = await store.undo_agent_cleanup(cleanup_id)
    assert undone.reactivated == 1
    with pytest.raises(RestoreChainConflict) as exc:
        await store.restore_entry(good.id)
    assert "new memory" in str(exc.value)
    assert (await _state(adapter, good.id)).state is EntryState.WITHDRAWN
    assert (await _state(adapter, junk.id)).state is EntryState.ACTIVE


async def test_restore_scenario_a_cannot_end_with_two_active_heads(adapter: Adapter) -> None:
    """The successor is withdrawn by hand, then good is restored, then the
    successor: neither step may leave both active."""
    store = adapter.store
    good, junk = await _hand_withdrawn_and_cleanup_undone(adapter)
    await store.withdraw_entry(junk.id, "by hand", by_user="alice")
    with pytest.raises(RestoreChainConflict):
        await store.restore_entry(good.id)
    # junk is a plain withdrawn head with no link: restoring it is the only way back.
    await store.restore_entry(junk.id)
    states = {(await _state(adapter, e.id)).state for e in (good, junk)}
    assert states == {EntryState.WITHDRAWN, EntryState.ACTIVE}


async def test_restore_scenario_b_a_newer_cleanup_then_its_undo_cannot_make_two_heads(
    adapter: Adapter,
) -> None:
    store = adapter.store
    good, junk = await _hand_withdrawn_and_cleanup_undone(adapter)
    newer = await _clean(adapter)  # C3 withdraws junk again
    assert newer.record is not None
    with pytest.raises(RestoreChainConflict):
        await store.restore_entry(good.id)
    await store.undo_agent_cleanup(newer.record.id)
    states = {(await _state(adapter, e.id)).state for e in (good, junk)}
    assert states == {EntryState.WITHDRAWN, EntryState.ACTIVE}
