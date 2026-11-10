"""Fleet merges, moves and unmerges."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from typing import NamedTuple

from hivemind.domain.access import Fleet, FleetExists, UnknownFleet
from hivemind.domain.entry import Entry, EntryState
from hivemind.domain.fleet_merge import (
    KIND_MOVE,
    DroppedPin,
    DuplicatePair,
    DuplicateScan,
    FleetMergeRecord,
    FleetMergeRefused,
    FleetMoveRefused,
    MergeOutcome,
    MoveOutcome,
    MovePlan,
    UnknownFleetMerge,
    UnmergeOutcome,
    check_pins_unchanged,
    choose_pins,
    plan_move_entries,
    plan_pin_restore,
    undo_refusal,
)
from hivemind.domain.pin import Pin
from hivemind.domain.validation import InvalidInput, is_valid_uuid
from hivemind.memstore.helpers import cosine_similarity
from hivemind.memstore.state import MemoryState


def _require_one_target(target_fleet_id: str | None, new_fleet_name: str | None) -> None:
    if (target_fleet_id is None) == (new_fleet_name is None):
        raise InvalidInput("name exactly one of target_fleet_id and new_fleet_name")


def _canonical_fleet_ids(source_id: str, target_fleet_id: str | None) -> tuple[str, str | None]:
    """The merge or move's fleet ids as canonical uuid text (``UnknownFleet`` for a non-uuid)."""
    for given in (source_id, target_fleet_id):
        if given is not None and not is_valid_uuid(given):
            raise UnknownFleet(f"unknown fleet: {given}")
    return str(uuid.UUID(source_id)), (
        str(uuid.UUID(target_fleet_id)) if target_fleet_id is not None else None
    )


def _dropped_pins(dropped: Sequence[Pin]) -> tuple[DroppedPin, ...]:
    return tuple(DroppedPin(p.fleet_id, p.entry_id, p.pinned_by, p.pinned_at) for p in dropped)


class _PinsBack(NamedTuple):
    """What an undo did with pins: how many returned, and the lists it reports."""

    returned_count: int
    restored: list[DroppedPin]
    skipped: list[DroppedPin]
    unpinned: list[DroppedPin]


class FleetMergeStore(MemoryState):
    async def duplicate_pairs(
        self, fleet_id: str, other_id: str, threshold: float, cap: int
    ) -> DuplicateScan:
        with self._lock:
            active = [
                e
                for e in self._entries.values()
                if e.state is EntryState.ACTIVE and e.embedding is not None
            ]
            mine = sorted(
                (e for e in active if e.fleet_id == fleet_id),
                key=lambda e: (-e.created_at.timestamp(), e.id),
            )
            theirs = [e for e in active if e.fleet_id == other_id]
        looked = mine[:cap]
        pairs: list[DuplicatePair] = []
        for e in looked:
            best: tuple[float, datetime, str] | None = None
            for o in theirs:
                sim = cosine_similarity(list(e.embedding or ()), list(o.embedding or ()))
                key = (-sim, o.created_at, o.id)
                if best is None or key < (-best[0], best[1], best[2]):
                    best = (sim, o.created_at, o.id)
            if best is not None and best[0] >= threshold:
                pairs.append(DuplicatePair(e.id, best[2], best[0]))
        pairs.sort(key=lambda p: (-p.similarity, p.entry_id))
        return DuplicateScan(tuple(pairs), len(looked), len(mine) > cap)

    def _resolve_sides(
        self,
        source_id: str,
        target_fleet_id: str | None,
        new_fleet_name: str | None,
        into_itself: Exception,
    ) -> tuple[Fleet, Fleet | None]:
        """The source and the existing target (``None`` when ``new_fleet_name``
        names a fleet to create) of a merge or move, refusing an unknown or
        merged side, a taken name, and a target that is the source
        (``into_itself``). Call with ``self._lock`` held."""
        source = self._fleets.get(source_id)
        if source is None:
            raise UnknownFleet(f"unknown fleet: {source_id}")
        target = None
        if target_fleet_id is not None:
            target = self._fleets.get(target_fleet_id)
            if target is None:
                raise UnknownFleet(f"unknown fleet: {target_fleet_id}")
            if target.id == source.id:
                raise into_itself
        elif any(f.name == new_fleet_name for f in self._fleets.values()):
            raise FleetExists(f"fleet already exists: {new_fleet_name!r}")
        for side in (source, target):
            if side is not None and side.merged_into is not None:
                self._refuse_merged(side.id)
        return source, target

    async def merge_fleets(
        self,
        source_id: str,
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
        merged_by: str,
    ) -> MergeOutcome:
        _require_one_target(target_fleet_id, new_fleet_name)
        source_id, target_fleet_id = _canonical_fleet_ids(source_id, target_fleet_id)
        with self._lock:
            source, target = self._resolve_sides(
                source_id,
                target_fleet_id,
                new_fleet_name,
                FleetMergeRefused("a fleet cannot be merged into itself"),
            )
            pins = [
                p
                for (f, _), p in self._pins.items()
                if f == source.id or (target is not None and f == target.id)
            ]
            check_pins_unchanged(pins, expected_pin_ids)
            kept, dropped = choose_pins(pins, keep_pin_ids)
            # Nothing below can fail: mutate.
            created = target is None
            if target is None:
                target = self._create_target_fleet(new_fleet_name)
            now = self._clock()
            moved = [e.id for e in self._entries.values() if e.fleet_id == source.id]
            for entry_id in moved:
                self._entries[entry_id] = replace(self._entries[entry_id], fleet_id=target.id)
            agents = sorted(a.name for a in self._agents.values() if a.home_fleet_id == source.id)
            for name in agents:
                self._agents[name] = replace(self._agents[name], home_fleet_id=target.id)
            drops = _dropped_pins(dropped)
            rekeyed = self._rekey_pins(pins, kept, target.id)
            if source.id in self._searches:
                mine = self._searches.pop(source.id)
                theirs = self._searches.get(target.id, (0, 0, 0))
                self._searches[target.id] = tuple(a + b for a, b in zip(mine, theirs, strict=True))  # type: ignore[assignment]
            self._fleets[source.id] = replace(source, merged_into=target.id, merged_at=now)
            record = FleetMergeRecord(
                id=str(uuid.uuid4()),
                source_fleet_id=source.id,
                target_fleet_id=target.id,
                merged_by=merged_by,
                merged_at=now,
                moved_agents=tuple(agents),
                dropped_pins=drops,
            )
            self._record_merge(record, moved)
            return MergeOutcome(record, target, created, len(moved), len(agents), rekeyed, drops)

    def _move_entries(self, source_id: str, agents: Sequence[str]) -> tuple[list[Entry], int]:
        return plan_move_entries(self._entries.values(), source_id, agents)

    async def plan_fleet_move(self, source_id: str, agents: Sequence[str]) -> MovePlan:
        with self._lock:
            moved, held = self._move_entries(source_id, agents)
        return MovePlan(
            tuple(e.id for e in moved), dict(Counter(e.state.value for e in moved)), held
        )

    async def move_agents(
        self,
        source_id: str,
        agents: Sequence[str],
        *,
        target_fleet_id: str | None = None,
        new_fleet_name: str | None = None,
        keep_pin_ids: Sequence[str] | None = None,
        expected_pin_ids: Sequence[str] | None = None,
        moved_by: str,
    ) -> MoveOutcome:
        _require_one_target(target_fleet_id, new_fleet_name)
        names = sorted(set(agents))
        if not names:
            raise InvalidInput("name at least one agent to move")
        source_id, target_fleet_id = _canonical_fleet_ids(source_id, target_fleet_id)
        with self._lock:
            source, target = self._resolve_sides(
                source_id,
                target_fleet_id,
                new_fleet_name,
                FleetMoveRefused("agents cannot be moved into the fleet they are in"),
            )
            self._require_source_agents(source, names)
            moving, held = self._move_entries(source.id, names)
            pins = self._move_pin_candidates(source, target, {e.id for e in moving})
            check_pins_unchanged(pins, expected_pin_ids, FleetMoveRefused)
            kept, dropped = choose_pins(pins, keep_pin_ids)
            # Nothing below can fail: mutate.
            created = target is None
            if target is None:
                target = self._create_target_fleet(new_fleet_name)
            now = self._clock()
            for e in moving:
                self._entries[e.id] = replace(e, fleet_id=target.id)
            for name in names:
                self._agents[name] = replace(self._agents[name], home_fleet_id=target.id)
            drops = _dropped_pins(dropped)
            rekeyed = self._rekey_pins(pins, kept, target.id)
            record = FleetMergeRecord(
                id=str(uuid.uuid4()),
                source_fleet_id=source.id,
                target_fleet_id=target.id,
                merged_by=moved_by,
                merged_at=now,
                moved_agents=tuple(names),
                dropped_pins=drops,
                kind=KIND_MOVE,
            )
            self._record_merge(record, [e.id for e in moving])
            return MoveOutcome(
                record, target, created, len(moving), held, len(names), rekeyed, drops
            )

    async def unmerge_fleet(self, merge_id: str) -> UnmergeOutcome:
        if not is_valid_uuid(merge_id):
            raise UnknownFleetMerge(f"unknown fleet merge: {merge_id}")
        merge_id = str(uuid.UUID(merge_id))
        with self._lock:
            record = self._load_undoable_merge(merge_id)
            source = self._fleets[record.source_fleet_id]
            target = self._fleets[record.target_fleet_id]
            moved = set(self._merge_entries[record.id])
            back = [
                eid
                for eid in self._merge_entries[record.id]
                if self._entries[eid].fleet_id == target.id
            ]
            for eid in back:
                self._entries[eid] = replace(self._entries[eid], fleet_id=source.id)
            agents = [
                n
                for n in record.moved_agents
                if n in self._agents and self._agents[n].home_fleet_id == target.id
            ]
            for name in agents:
                self._agents[name] = replace(self._agents[name], home_fleet_id=source.id)
            # Pins follow their entries back.
            if record.kind == KIND_MOVE:
                pins = self._return_moved_pins(record, source, target, back)
            else:
                returned_keys = [
                    key for key in list(self._pins) if key[0] == target.id and key[1] in moved
                ]
                for key in returned_keys:
                    pin = self._pins.pop(key)
                    self._pins[(source.id, key[1])] = replace(pin, fleet_id=source.id)
                pins = _PinsBack(len(returned_keys), [], [], [])
            self._restore_dropped_pins(record, source, target, pins)
            now = self._clock()
            if record.kind != KIND_MOVE:  # a move never marked its source
                self._fleets[source.id] = replace(source, merged_into=None, merged_at=None)
            done = replace(record, undone_at=now)
            self._merges[self._merges.index(record)] = done
            return UnmergeOutcome(
                done,
                len(back),
                tuple(agents),
                pins.returned_count,
                tuple(pins.restored),
                tuple(pins.skipped),
                tuple(pins.unpinned),
            )

    def _load_undoable_merge(self, merge_id: str) -> FleetMergeRecord:
        """The merge or move's record; refused when it cannot be undone. Callers hold the lock."""
        record = next((m for m in self._merges if m.id == merge_id), None)
        if record is None:
            raise UnknownFleetMerge(f"unknown fleet merge: {merge_id}")
        source = self._fleets[record.source_fleet_id]
        target = self._fleets[record.target_fleet_id]
        reason = undo_refusal(record, list(reversed(self._merges)), target, source)
        if reason is not None:
            raise (FleetMoveRefused if record.kind == KIND_MOVE else FleetMergeRefused)(reason)
        return record

    def _return_moved_pins(
        self, record: FleetMergeRecord, source: Fleet, target: Fleet, back: list[str]
    ) -> _PinsBack:
        """Undo of a move: the pins on the moved entries go home to the source, and the
        source's dropped pins are re-pinned, both under the source's cap. Callers hold the lock."""
        restored: list[DroppedPin] = []
        skipped: list[DroppedPin] = []
        unpinned: list[DroppedPin] = []
        # A move's pins that come back face the source's cap like any
        # restored pin (the source may have refilled its briefing).
        returning = [
            DroppedPin(target.id, key[1], pin.pinned_by, pin.pinned_at)
            for key, pin in self._pins.items()
            if key[0] == target.id and key[1] in back
        ]
        mine = [d for d in record.dropped_pins if d.fleet_id == source.id]
        back_pinned = {d.entry_id for d in returning}  # already coming back
        fits = [
            d
            for d in mine
            if (e := self._entries.get(d.entry_id)) is not None
            and e.fleet_id == source.id
            and (source.id, d.entry_id) not in self._pins
            and d.entry_id not in back_pinned
        ]
        skipped.extend(d for d in mine if d not in fits and d.entry_id not in back_pinned)
        held = [p.pinned_by for (f, _), p in self._pins.items() if f == source.id]
        put, over = plan_pin_restore([*returning, *fits], held)
        for d in sorted(put, key=lambda d: d.pinned_at):
            if d in returning:
                self._pins.pop((target.id, d.entry_id))
            else:
                restored.append(d)
            self._pins[(source.id, d.entry_id)] = Pin(
                source.id, d.entry_id, d.pinned_by, d.pinned_at
            )
        for d in over:
            if d in returning:
                self._pins.pop((target.id, d.entry_id))
                unpinned.append(d)
            skipped.append(d)
        return _PinsBack(sum(1 for d in put if d in returning), restored, skipped, unpinned)

    def _restore_dropped_pins(
        self, record: FleetMergeRecord, source: Fleet, target: Fleet, pins: _PinsBack
    ) -> None:
        """Re-pin the pins the merge or move dropped, fleet by fleet, into ``pins``.
        Callers hold the lock."""
        for fleet in (target,) if record.kind == KIND_MOVE else (source, target):
            mine = [d for d in record.dropped_pins if d.fleet_id == fleet.id]
            put, left = self._restore_pins(fleet.id, mine, lambda e: True)
            pins.restored.extend(put)
            pins.skipped.extend(left)

    def _create_target_fleet(self, new_fleet_name: str | None) -> Fleet:
        """Create the new fleet a merge or move targets. Callers hold the lock."""
        assert new_fleet_name is not None
        target = Fleet(id=str(uuid.uuid4()), name=new_fleet_name, created_at=self._clock())
        self._fleets[target.id] = target
        return target

    def _rekey_pins(
        self, pins: Sequence[Pin], kept: Sequence[Pin], target_id: str
    ) -> tuple[Pin, ...]:
        """Take ``pins`` off their fleets and put ``kept`` into the target; returns the
        re-keyed pins. Callers hold the lock."""
        for p in pins:
            self._pins.pop((p.fleet_id, p.entry_id), None)
        rekeyed = tuple(replace(p, fleet_id=target_id) for p in kept)
        for p in sorted(rekeyed, key=lambda p: p.pinned_at):
            self._pins[(p.fleet_id, p.entry_id)] = p
        return rekeyed

    def _record_merge(self, record: FleetMergeRecord, entry_ids: list[str]) -> None:
        """Callers hold the lock."""
        self._merges.append(record)
        self._merge_entries[record.id] = entry_ids

    def _require_source_agents(self, source: Fleet, names: Sequence[str]) -> None:
        """``FleetMoveRefused`` unless every name is an agent of the source.
        Callers hold the lock."""
        away = [
            n for n in names if n not in self._agents or self._agents[n].home_fleet_id != source.id
        ]
        if away:
            raise FleetMoveRefused(
                f"not agents of fleet {source.name!r} (or unknown): " + ", ".join(away)
            )

    def _move_pin_candidates(
        self, source: Fleet, target: Fleet | None, moving_ids: set[str]
    ) -> list[Pin]:
        """The pins a move decides on: the moving entries' in the source, and the target's.
        Callers hold the lock."""
        return [
            p
            for (f, eid), p in self._pins.items()
            if (f == source.id and eid in moving_ids) or (target is not None and f == target.id)
        ]

    async def list_fleet_merges(self, limit: int) -> list[FleetMergeRecord]:
        with self._lock:
            return list(reversed(self._merges))[:limit]

    async def get_fleet_merge(self, merge_id: str) -> FleetMergeRecord | None:
        with self._lock:
            return next((m for m in self._merges if m.id == merge_id), None)
