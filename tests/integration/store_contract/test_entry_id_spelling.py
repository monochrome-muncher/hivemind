"""Any spelling of an entry id, and tie order: MemoryStore matches Postgres."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime

import pytest
from tests.integration.store_contract.helpers import ME, T0, Adapter, _fb, write

from hivemind.domain.entry import EntryFilters, EntryState
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.pin import PinWrite
from hivemind.memstore import MemoryStore
from hivemind.ports import EntryNotActiveConflict, SupersedeConflict, UnknownEntry


def _spellings(entry_id: str) -> list[str]:
    """Upper case and hyphen-less: both name the entry, as a ``uuid`` column reads them."""
    return [entry_id.upper(), entry_id.replace("-", ""), entry_id.upper().replace("-", "")]


async def test_a_write_names_its_targets_in_any_uuid_spelling(adapter: Adapter) -> None:
    store = adapter.store
    old = await write(adapter, "old")
    see = await write(adapter, "see")
    new = await write(adapter, "new", supersedes=(old.id.upper(),), see_also=(see.id.upper(),))
    flipped = await store.get_entry(old.id)
    assert flipped is not None
    assert flipped.state is EntryState.SUPERSEDED and flipped.superseded_by == new.id
    assert await store.entry_links(new.id, 10) == ([see.id], [])
    bare = await write(adapter, "next")
    again = await write(adapter, "again", supersedes=(bare.id.replace("-", ""),))
    bare_after = await store.get_entry(bare.id)
    assert bare_after is not None and bare_after.superseded_by == again.id
    # Two spellings of one target are one target.
    one = await write(adapter, "one")
    both = await write(adapter, "both", supersedes=(one.id, one.id.upper()))
    one_after = await store.get_entry(one.id)
    assert one_after is not None and one_after.superseded_by == both.id
    # A malformed id names no entry: the write is refused and nothing lands.
    live = await write(adapter, "live")
    with pytest.raises(SupersedeConflict):
        await write(adapter, "bad", supersedes=(live.id, "not-a-uuid"))
    live_after = await store.get_entry(live.id)
    assert live_after is not None and live_after.state is EntryState.ACTIVE


async def test_reads_by_entry_id_accept_any_uuid_spelling(adapter: Adapter) -> None:
    store = adapter.store
    a = await write(adapter, "a", hot=0)
    b = await write(adapter, "b", hot=1)
    c = await write(adapter, "c", hot=2, supersedes=(b.id,), see_also=(a.id,))
    await _fb(adapter, a, ME, Verdict.WRONG, T0)
    for spelled in _spellings(a.id):
        assert await store.feedback_counts(spelled) == (0, 0, 1)
        assert [f.entry_id for f in await store.list_feedback(spelled, 10)] == [a.id]
        assert (await store.quality_counts([spelled]))[a.id] == (0, 0, 1)
        assert (await store.feedback_signals([spelled]))[a.id].wrong == 1
        assert await store.entry_links(spelled, 10) == ([], [c.id])
        assert (await store.entry_links_many([spelled], 10))[spelled] == ([], [c.id])
        assert (await store.list_feedback_many([spelled], 10))[spelled][0].entry_id == a.id
    pair = tuple(sorted((a.id, c.id)))
    for spelled in _spellings(a.id):
        sims = await store.pairwise_similarity([spelled, c.id.upper()])
        assert list(sims) == [pair]
    for spelled in _spellings(c.id):
        near = await store.similar_entries(adapter.vec(2), 10, exclude_id=spelled)
        assert c.id not in [eid for eid, _ in near]
    for spelled in _spellings(c.id):
        assert [e.id for e in await store.list_predecessors([spelled])] == [b.id]
    signals = await store.feedback_signals([c.id], count_supersedes=True)
    assert signals[c.id].supersedes == 1
    # Malformed ids are simply not found.
    assert await store.feedback_counts("not-a-uuid") == (0, 0, 0)
    assert await store.list_feedback("not-a-uuid", 10) == []
    assert await store.entry_links("not-a-uuid", 10) == ([], [])


async def test_feedback_recorded_under_any_spelling_lands_on_the_entry(adapter: Adapter) -> None:
    store = adapter.store
    a = await write(adapter, "a")
    await store.record_feedback(Feedback(a.id.upper(), "u", "x", Verdict.STALE, None, T0))
    await store.record_feedback(Feedback(a.id, "u", "x", Verdict.WRONG, None, T0))  # same row
    assert await store.feedback_counts(a.id) == (0, 0, 1)
    assert [f.entry_id for f in await store.list_feedback(a.id, 10)] == [a.id]


async def test_withdraw_accepts_any_uuid_spelling(adapter: Adapter) -> None:
    store = adapter.store
    a = await write(adapter, "a")
    withdrawn = await store.withdraw_entry(a.id.upper(), "gone", "boss")
    assert withdrawn.id == a.id and withdrawn.state is EntryState.WITHDRAWN
    with pytest.raises(EntryNotActiveConflict):
        await store.withdraw_entry(a.id.replace("-", ""), None, "boss")
    with pytest.raises(UnknownEntry):
        await store.withdraw_entry("not-a-uuid", None, "boss")


async def test_pins_accept_any_uuid_spelling(adapter: Adapter) -> None:
    store = adapter.store
    fleet = str(uuid.uuid4())
    a = await write(adapter, "a", scope="fleet", fleet_id=fleet)
    b = await write(adapter, "b", scope="fleet", fleet_id=fleet)
    first = await store.pin_entry(fleet, a.id.upper(), "boss", limit=9)
    assert isinstance(first, PinWrite) and first.created and first.pin.entry_id == a.id
    again = await store.pin_entry(fleet, a.id.replace("-", ""), "boss", limit=9)
    assert isinstance(again, PinWrite) and not again.created
    swapped = await store.pin_entry(fleet, b.id, "boss", limit=9, replaces=[a.id.upper()])
    assert isinstance(swapped, PinWrite) and swapped.replaced == (a.id.upper(),)  # echoed as asked
    assert [p.entry_id for p in await store.list_pins(fleet)] == [b.id]
    assert await store.unpin_entry(fleet, b.id.upper()) is True
    assert await store.unpin_entry(fleet, "not-a-uuid") is False


async def _same_instant(adapter: Adapter, table: str, at: datetime) -> None:
    """Give every pin (or link) one timestamp, so only the tie order is left."""
    store = adapter.store
    if isinstance(store, MemoryStore):
        if table == "pins":
            store._pins = {k: replace(p, pinned_at=at) for k, p in store._pins.items()}
        else:
            store._links = dict.fromkeys(store._links, at)
        return
    column = "pinned_at" if table == "pins" else "created_at"
    async with store._connection() as conn:  # type: ignore[attr-defined]
        await conn.execute(f"UPDATE {table} SET {column} = $1", at)


async def test_pins_with_one_timestamp_list_by_entry_id(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    entries = [await write(adapter, f"p{i}", scope="fleet", fleet_id=fleet) for i in range(4)]
    # Pin in id order, so the oldest-first order of insertion is not the tie order.
    for e in sorted(entries, key=lambda e: e.id):
        assert isinstance(await adapter.store.pin_entry(fleet, e.id, "boss", limit=9), PinWrite)
    await _same_instant(adapter, "pins", T0)
    got = [p.entry_id for p in await adapter.store.list_pins(fleet)]
    assert got == sorted(e.id for e in entries)


async def test_links_with_one_timestamp_list_by_the_other_end_id(adapter: Adapter) -> None:
    target = await write(adapter, "target")
    other = await write(adapter, "other")
    sources = [await write(adapter, f"s{i}", see_also=(target.id, other.id)) for i in range(4)]
    await _same_instant(adapter, "entry_links", T0)
    ids = sorted(s.id for s in sources)
    assert await adapter.store.entry_links(target.id, 10) == ([], ids)
    assert await adapter.store.entry_links(target.id, 2) == ([], ids[:2])
    out, _ = await adapter.store.entry_links(sources[0].id, 10)
    assert out == sorted([target.id, other.id])


async def test_supersede_of_a_fleet_target_from_another_fleet_is_refused(
    adapter: Adapter,
) -> None:
    # The target's fleet changed after the service's may_supersede check.
    old_fleet, other_fleet = str(uuid.uuid4()), str(uuid.uuid4())
    target = await write(adapter, "fleet fact", scope="fleet", fleet_id=old_fleet)
    before = await adapter.store.count_entries(EntryFilters(include_inactive=True))
    for scope, fleet in (("fleet", other_fleet), ("fleet", None), ("self", None)):
        with pytest.raises(SupersedeConflict) as exc:
            await write(adapter, "successor", scope=scope, fleet_id=fleet, supersedes=(target.id,))
        assert exc.value.ids == [target.id]
    untouched = await adapter.store.get_entry(target.id)
    assert untouched is not None and untouched.state is EntryState.ACTIVE
    assert await adapter.store.count_entries(EntryFilters(include_inactive=True)) == before
    # Its own fleet, and an org successor (the admin's), still supersede.
    same = await write(adapter, "v2", scope="fleet", fleet_id=old_fleet, supersedes=(target.id,))
    assert same.id
    other = await write(adapter, "other", scope="fleet", fleet_id=other_fleet)
    await write(adapter, "org v2", scope="org", supersedes=(other.id,))
    flipped = await adapter.store.get_entry(other.id)
    assert flipped is not None and flipped.state is EntryState.SUPERSEDED


async def test_supersede_of_self_and_org_targets_ignores_the_successor_fleet(
    adapter: Adapter,
) -> None:
    mine = await write(adapter, "note", scope="self")
    org = await write(adapter, "org fact", scope="org")
    await write(
        adapter,
        "v2",
        scope="fleet",
        fleet_id=str(uuid.uuid4()),
        supersedes=(mine.id, org.id),
    )
    for e in (mine, org):
        got = await adapter.store.get_entry(e.id)
        assert got is not None and got.state is EntryState.SUPERSEDED
