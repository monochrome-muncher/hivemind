"""Fleet pins."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from hivemind.domain.pin import Pin, PinRefusal, PinWrite, pin_refusal
from hivemind.domain.validation import is_valid_uuid
from hivemind.memstore.helpers import canonical_or_same
from hivemind.memstore.state import MemoryState


class PinStore(MemoryState):
    async def pin_entry(
        self,
        fleet_id: str,
        entry_id: str,
        pinned_by: str,
        limit: int,
        replaces: Sequence[str] = (),
        agent_limit: int | None = None,
        also_owned_by: Sequence[str] = (),
    ) -> PinWrite | PinRefusal:
        if not is_valid_uuid(entry_id):
            return PinRefusal.FLEET  # as on Postgres: nothing a malformed id can pin
        entry_id = str(uuid.UUID(entry_id))
        asked = list(dict.fromkeys(replaces))  # reported in the caller's spelling
        replaces = [canonical_or_same(r) for r in replaces]
        with self._lock:
            self._refuse_merged(fleet_id)
            self._refuse_stale_home(fleet_id, pinned_by)
            fleet_pins = [p for (f, _), p in self._pins.items() if f == fleet_id]
            refusal = pin_refusal(
                fleet_pins, entry_id, pinned_by, limit, agent_limit, replaces, also_owned_by
            )
            if refusal is not None:
                return refusal
            existing = self._pins.get((fleet_id, entry_id))
            replaced = [
                old
                for old in asked
                if (key := canonical_or_same(old)) != entry_id
                and self._pins.pop((fleet_id, key), None) is not None
            ]
            self._pins.pop((fleet_id, entry_id), None)
            pin = Pin(fleet_id, entry_id, pinned_by, self._clock())
            self._pins[(fleet_id, entry_id)] = pin
            return PinWrite(pin, existing is None, tuple(replaced))

    async def unpin_entry(self, fleet_id: str, entry_id: str) -> bool:
        if not is_valid_uuid(entry_id):
            return False
        with self._lock:
            return self._pins.pop((fleet_id, str(uuid.UUID(entry_id))), None) is not None

    async def list_pins(self, fleet_id: str) -> list[Pin]:
        with self._lock:
            pins = [p for (f, _), p in self._pins.items() if f == fleet_id]
        # PgStore's ``ORDER BY pinned_at DESC, entry_id``.
        pins.sort(key=lambda p: p.entry_id)
        pins.sort(key=lambda p: p.pinned_at, reverse=True)
        return pins
