"""Pins (ADR 0058) on both adapters."""

from __future__ import annotations

import uuid

from tests.integration.store_contract.helpers import Adapter, write

from hivemind.domain.pin import PinRefusal, PinWrite


async def test_pins_are_refreshed_capped_and_newest_first(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    a = await write(adapter, "a", scope="fleet", fleet_id=fleet)
    b = await write(adapter, "b", scope="fleet", fleet_id=fleet)
    c = await write(adapter, "c", scope="fleet", fleet_id=fleet)
    first = await adapter.store.pin_entry(fleet, a.id, "boss", limit=2)
    assert first is not None and first.created and first.replaced == ()
    assert first.pin.entry_id == a.id and first.pin.pinned_by == "boss"
    assert await adapter.store.pin_entry(fleet, b.id, "boss", limit=2) is not None
    assert await adapter.store.pin_entry(fleet, c.id, "boss", limit=2) is PinRefusal.FLEET
    assert await adapter.store.pin_entry(fleet, "not-a-uuid", "boss", limit=9) is PinRefusal.FLEET
    assert [p.entry_id for p in await adapter.store.list_pins(fleet)] == [b.id, a.id]
    # Pinning a pinned entry refreshes it (ADR 0076): same row, new pinner,
    # back to the front, and a full fleet still accepts it.
    again = await adapter.store.pin_entry(fleet, a.id, "other", limit=2)
    assert again is not None and not again.created and again.pin.pinned_by == "other"
    assert again.pin.pinned_at >= first.pin.pinned_at
    assert [p.entry_id for p in await adapter.store.list_pins(fleet)] == [a.id, b.id]
    assert await adapter.store.unpin_entry(fleet, a.id) is True
    assert await adapter.store.unpin_entry(fleet, a.id) is False
    assert await adapter.store.list_pins(str(uuid.uuid4())) == []


async def test_a_pin_replaces_the_older_pins_it_names(adapter: Adapter) -> None:
    fleet = str(uuid.uuid4())
    a = await write(adapter, "a", scope="fleet", fleet_id=fleet)
    b = await write(adapter, "b", scope="fleet", fleet_id=fleet)
    new = await write(adapter, "ab", scope="fleet", fleet_id=fleet)
    gone = str(uuid.uuid4())
    for entry in (a, b):
        assert await adapter.store.pin_entry(fleet, entry.id, "boss", limit=2) is not None
    # A full fleet may still swap pins: the count does not grow.
    moved = await adapter.store.pin_entry(
        fleet, new.id, "lead", limit=2, replaces=[a.id, b.id, gone, new.id]
    )
    assert moved is not None and moved.created and set(moved.replaced) == {a.id, b.id}
    assert [p.entry_id for p in await adapter.store.list_pins(fleet)] == [new.id]
    assert await adapter.store.unpin_entry(fleet, a.id) is False


async def test_a_pin_is_capped_per_agent_and_a_refusal_writes_nothing(adapter: Adapter) -> None:
    """ADR 0096: an agent holds at most ``agent_limit`` of the fleet's pins;
    a refresh restamps the pinner so it counts, a move onto a new version
    does not raise the count, and ``None`` (the admin) is exempt."""
    fleet = str(uuid.uuid4())
    store = adapter.store
    es = [await write(adapter, f"e{i}", scope="fleet", fleet_id=fleet) for i in range(5)]
    for e in es[:2]:
        assert isinstance(await store.pin_entry(fleet, e.id, "ann", 10, agent_limit=2), PinWrite)
    # A third pin by ann breaks her cap; the fleet cap (10) is not the one hit.
    refused = await store.pin_entry(fleet, es[2].id, "ann", 10, agent_limit=2)
    assert refused is PinRefusal.AGENT
    assert [p.entry_id for p in await store.list_pins(fleet)] == [es[1].id, es[0].id]
    # Bob has his own count; pinning an entry ann holds is a refresh that takes
    # it over to him, which frees her slot.
    assert isinstance(await store.pin_entry(fleet, es[2].id, "bob", 10, agent_limit=2), PinWrite)
    assert isinstance(await store.pin_entry(fleet, es[0].id, "bob", 10, agent_limit=2), PinWrite)
    # Bob now holds two; a refresh of ann's other pin would be his third.
    assert await store.pin_entry(fleet, es[1].id, "bob", 10, agent_limit=2) is PinRefusal.AGENT
    assert {p.entry_id: p.pinned_by for p in await store.list_pins(fleet)} == {
        es[0].id: "bob",
        es[1].id: "ann",
        es[2].id: "bob",
    }
    # Refreshing one's own pin at the cap is fine.
    assert isinstance(await store.pin_entry(fleet, es[0].id, "bob", 10, agent_limit=2), PinWrite)
    # A move onto a new version removes the caller's pin and adds one: allowed
    # at the cap; but a move that does not free one of the caller's slots is not.
    moved = await store.pin_entry(fleet, es[3].id, "bob", 10, replaces=[es[2].id], agent_limit=2)
    assert isinstance(moved, PinWrite) and moved.replaced == (es[2].id,)
    refused = await store.pin_entry(fleet, es[4].id, "bob", 10, replaces=[es[1].id], agent_limit=2)
    assert refused is PinRefusal.AGENT  # es[1] is ann's: bob would hold three
    assert {p.entry_id for p in await store.list_pins(fleet)} == {es[0].id, es[1].id, es[3].id}
    # No agent limit (the admin): exempt, though the fleet cap still applies.
    assert await store.pin_entry(fleet, es[4].id, "admin", 3) is PinRefusal.FLEET
    assert isinstance(await store.pin_entry(fleet, es[4].id, "admin", 4), PinWrite)
