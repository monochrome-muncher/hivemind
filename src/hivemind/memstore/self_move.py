"""Moving a fleet memory to `self` (ADR 0145), as ``PgStore`` does it."""

from __future__ import annotations

from dataclasses import replace

from hivemind.domain.access import SCOPE_FLEET, SCOPE_SELF
from hivemind.domain.self_move import (
    RemovedPin,
    SelfMove,
    replaced_others_refusal,
    self_move_refusal,
)
from hivemind.domain.validation import canonical_uuid
from hivemind.memstore.helpers import copy_entry
from hivemind.memstore.state import MemoryState


class SelfMoveStore(MemoryState):
    async def move_entry_to_self(self, entry_id: str) -> SelfMove:
        key = canonical_uuid(entry_id)
        if key is None:
            raise KeyError(entry_id)
        with self._lock:
            head = self._entries.get(key)
            if head is None:
                raise KeyError(entry_id)
            self._refuse_merged(head.fleet_id)
            refusal = self_move_refusal(key, head.state.value, head.scope)
            if refusal is not None:
                raise refusal
            owners = {head.author, *self._inherited(head.author)}
            own = self._own_fleet_versions(key, owners)
            others = [
                e.id
                for e in self._entries.values()
                if e.superseded_by in own and e.author not in owners and e.scope == SCOPE_FLEET
            ]
            refusal = replaced_others_refusal(key, others)
            if refusal is not None:
                raise refusal
            for eid in own:
                self._entries[eid] = replace(self._entries[eid], scope=SCOPE_SELF)
            unpinned = sorted(RemovedPin(fleet, eid) for (fleet, eid) in self._pins if eid in own)
            for pin in unpinned:
                del self._pins[(pin.fleet_id, pin.entry_id)]
            return SelfMove(
                copy_entry(self._entries[key]),
                tuple(sorted(own - {key})),
                tuple(unpinned),
            )

    def _own_fleet_versions(self, head_id: str, owners: set[str]) -> set[str]:
        own = {head_id}
        frontier = [head_id]
        while frontier:
            current = frontier.pop()
            for e in self._entries.values():
                if (
                    e.superseded_by == current
                    and e.author in owners
                    and e.scope == SCOPE_FLEET
                    and e.id not in own
                ):
                    own.add(e.id)
                    frontier.append(e.id)
        return own
