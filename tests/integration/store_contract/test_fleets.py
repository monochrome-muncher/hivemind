"""Fleets (ADRs 0011, 0012, 0028, 0045) on both adapters: renames and phases."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.store_contract.helpers import NO_SUCH_ID, Adapter, _fleet_entry, _two_fleets

from hivemind.domain.access import FleetExists, FleetMerged, UnknownFleet
from hivemind.domain.fleet_phase import FleetPhase, PhaseName


async def test_fleets(adapter: Adapter) -> None:
    fleet = await adapter.store.create_fleet("blue")
    with pytest.raises(FleetExists, match="fleet already exists: 'blue'"):
        await adapter.store.create_fleet("blue")
    assert await adapter.store.get_fleet(fleet.id) == fleet
    assert await adapter.store.get_fleet(NO_SUCH_ID) is None
    assert [f.name for f in await adapter.store.list_fleets()] == ["blue"]


async def test_rename_fleet(adapter: Adapter) -> None:
    store = adapter.store
    a, _ = await _two_fleets(adapter)
    entry = await _fleet_entry(adapter, "kept", a)
    before, after = await store.rename_fleet(a, "alpha")
    assert (before.name, after.name, after.id) == ("a", "alpha", a)
    assert (after.created_at, after.merged_into) == (before.created_at, None)
    assert await store.get_fleet(a) == after
    assert [f.name for f in await store.list_fleets()] == ["alpha", "b"]
    got = await store.get_entry(entry.id)  # the id never changes, so memories stay put
    assert got is not None and got.fleet_id == a
    # The current name again is a no-op; a taken name (exact match) is refused.
    assert await store.rename_fleet(a, "alpha") == (after, after)
    with pytest.raises(FleetExists, match="fleet already exists: 'b'"):
        await store.rename_fleet(a, "b")
    assert (await store.get_fleet(a)) == after
    await store.rename_fleet(a, "B")  # names are case-sensitive, as create is
    with pytest.raises(UnknownFleet):
        await store.rename_fleet(NO_SUCH_ID, "x")
    with pytest.raises(UnknownFleet):
        await store.rename_fleet("not-a-uuid", "x")


async def test_rename_a_merged_fleet(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")
    _, after = await store.rename_fleet(a, "a (merged)")
    assert (after.name, after.merged_into) == ("a (merged)", b)
    assert await store.get_fleet(a) == after


async def test_set_and_clear_a_fleet_phase(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    until = datetime.now(UTC) + timedelta(days=5)
    explore = FleetPhase(PhaseName.EXPLORE, until)
    before, after = await store.set_fleet_phase(a, explore)
    assert before.phase is None
    assert after.phase == explore
    assert await store.get_fleet(a) == after
    assert (await store.get_fleet(b)).phase is None  # type: ignore[union-attr]
    was, replaced = await store.set_fleet_phase(a, FleetPhase(PhaseName.CONSOLIDATE, until))
    assert was.phase == explore
    assert replaced.phase == FleetPhase(PhaseName.CONSOLIDATE, until)
    assert [f.phase for f in await store.list_fleets()] == [replaced.phase, None]
    was, cleared = await store.set_fleet_phase(a, None)
    assert was.phase == replaced.phase
    assert cleared.phase is None
    assert await store.get_fleet(a) == cleared
    with pytest.raises(UnknownFleet):
        await store.set_fleet_phase(NO_SUCH_ID, explore)
    with pytest.raises(UnknownFleet):
        await store.set_fleet_phase("not-a-uuid", explore)


async def test_a_merge_clears_the_source_phase_and_an_undo_does_not_bring_it_back(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    until = datetime.now(UTC) + timedelta(days=5)
    await store.set_fleet_phase(a, FleetPhase(PhaseName.EXPLORE, until))
    target_phase = FleetPhase(PhaseName.CONSOLIDATE, until)
    await store.set_fleet_phase(b, target_phase)
    merged = await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")
    assert (await store.get_fleet(a)).phase is None  # type: ignore[union-attr]
    await store.unmerge_fleet(merged.record.id)
    restored = await store.get_fleet(a)
    assert restored is not None and restored.merged_into is None and restored.phase is None
    assert (await store.get_fleet(b)).phase == target_phase  # type: ignore[union-attr]


async def test_a_merged_fleet_takes_no_phase(adapter: Adapter) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")
    phase = FleetPhase(PhaseName.EXPLORE, datetime.now(UTC) + timedelta(days=5))
    with pytest.raises(FleetMerged) as exc:
        await store.set_fleet_phase(a, phase)
    assert exc.value.target_id == b
    merged = await store.get_fleet(a)
    assert merged is not None and merged.phase is None
