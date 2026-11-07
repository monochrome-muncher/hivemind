"""Restoring a withdrawn memory on Postgres (ADR 0124): the races at the store
seam. The refusals and the round trip run against both adapters in
``test_store_contract.py``; here concurrent callers meet the row lock."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from tests.integration.test_store_contract import (
    WIDE_SINCE,
    WIDE_UNTIL,
    Adapter,
    _junk,
    _pg_adapter,
    _state,
    write,
)

from hivemind.domain.entry import EntryState
from hivemind.domain.entry_restore import EntryNotWithdrawn, RestoreChainConflict
from hivemind.ports import SupersedeConflict

ROUNDS = 8


@pytest.fixture
async def pg() -> AsyncIterator[Adapter]:
    async for adapter in _pg_adapter():
        yield adapter


async def test_concurrent_restores_of_one_memory_succeed_exactly_once(pg: Adapter) -> None:
    for i in range(ROUNDS):
        entry = await write(pg, f"race {i}")
        await pg.store.withdraw_entry(entry.id, "r", by_user="alice")
        results = await asyncio.gather(
            *(pg.store.restore_entry(entry.id) for _ in range(4)), return_exceptions=True
        )
        wins = [r for r in results if not isinstance(r, BaseException)]
        losses = [r for r in results if isinstance(r, BaseException)]
        assert len(wins) == 1
        assert all(isinstance(e, EntryNotWithdrawn) for e in losses)
        assert (await _state(pg, entry.id)).state is EntryState.ACTIVE


async def test_a_restore_racing_a_supersede_never_leaves_two_heads(pg: Adapter) -> None:
    """The supersede needs an active target. Either it runs first (conflict, the
    memory is then restored) or after the restore (it replaces the memory).
    Never a withdrawn memory with a successor, never an active superseded one."""
    outcomes = set()
    for i in range(ROUNDS):
        head = await write(pg, f"head {i}")
        await pg.store.withdraw_entry(head.id, "r", by_user="alice")

        async def supersede(target: str = head.id, n: int = i) -> object:
            try:
                return await write(pg, f"next {n}", supersedes=(target,))
            except SupersedeConflict as exc:
                return exc

        restored, superseded = await asyncio.gather(pg.store.restore_entry(head.id), supersede())
        assert restored.entry.state is EntryState.ACTIVE
        after = await _state(pg, head.id)
        if isinstance(superseded, SupersedeConflict):
            assert after.state is EntryState.ACTIVE and after.superseded_by is None
            outcomes.add("restored first loses")
        else:
            assert after.state is EntryState.SUPERSEDED and after.superseded_by is not None
            assert after.withdrawn_reason is None
            outcomes.add("superseded after")
    assert outcomes  # either order is fine; the end states above are what matter


async def test_a_restore_racing_a_withdrawal_ends_in_one_state(pg: Adapter) -> None:
    """A withdraw of an active memory and a restore of it: the restore only
    applies once the memory is withdrawn, so with both in flight it is either
    refused (it ran first) or undoes the withdrawal (it ran second)."""
    for i in range(ROUNDS):
        entry = await write(pg, f"flip {i}")
        withdraw = pg.store.withdraw_entry(entry.id, "r", by_user="alice")
        restore = pg.store.restore_entry(entry.id)
        _, restored = await asyncio.gather(withdraw, restore, return_exceptions=True)
        state = (await _state(pg, entry.id)).state
        if isinstance(restored, EntryNotWithdrawn):
            assert state is EntryState.WITHDRAWN
        else:
            assert state is EntryState.ACTIVE
            assert not isinstance(restored, BaseException)


async def test_concurrent_restores_in_one_cleaned_chain_leave_one_head(pg: Adapter) -> None:
    """Two memories a cleanup put back behind the same successor, both then
    withdrawn by hand and the cleanup undone: restoring both at once must not
    end with two active heads in a chain that has an active successor."""
    store = pg.store
    a = await write(pg, "a", author="good")
    b = await write(pg, "b", author="good")
    junk = await _junk(pg, "junk", supersedes=(a.id, b.id))
    out = await store.cleanup_agent(
        "bad", WIDE_SINCE, WIDE_UNTIL, cleaned_by="admin:1", restore_ids=[a.id, b.id]
    )
    assert out.record is not None
    for entry in (a, b):
        await store.withdraw_entry(entry.id, "by hand", by_user="alice")
    await store.undo_agent_cleanup(out.record.id)
    assert (await _state(pg, junk.id)).state is EntryState.ACTIVE

    results = await asyncio.gather(
        store.restore_entry(a.id), store.restore_entry(b.id), return_exceptions=True
    )

    assert all(isinstance(r, RestoreChainConflict) for r in results)
    for entry in (a, b):
        assert (await _state(pg, entry.id)).state is EntryState.WITHDRAWN
