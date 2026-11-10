"""Pinned entries."""

from __future__ import annotations

from collections.abc import Sequence

from hivemind.domain.pin import Pin, PinRefusal, PinWrite, pin_refusal
from hivemind.domain.validation import is_valid_uuid
from hivemind.store.pgstore.connection import PgConnections
from hivemind.store.pgstore.fleet_guards import refuse_merged, refuse_stale_home
from hivemind.store.pgstore.pin_common import _PIN_COLUMNS, _PIN_ORDER, LOCK_FLEET_PINS
from hivemind.store.pgstore.rows import row_to_pin

INSERT_PIN = """
INSERT INTO pins (fleet_id, entry_id, pinned_by) VALUES ($1, $2, $3)
ON CONFLICT (fleet_id, entry_id) DO UPDATE SET pinned_by = EXCLUDED.pinned_by, pinned_at = now()
RETURNING fleet_id, entry_id, pinned_by, pinned_at, (xmax = 0) AS created
"""

DELETE_PIN = "DELETE FROM pins WHERE fleet_id = $1 AND entry_id = $2"

LIST_PINS = f"""
SELECT {_PIN_COLUMNS} FROM pins WHERE fleet_id = $1
 ORDER BY {_PIN_ORDER}
"""


class PinStore(PgConnections):
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
            return PinRefusal.FLEET
        async with self._write_transaction(fleet_id) as conn:
            # The fleet row first, then the pin lock: the order a merge takes them.
            await refuse_merged(conn, fleet_id)
            await refuse_stale_home(conn, fleet_id, pinned_by)
            await conn.execute(LOCK_FLEET_PINS, fleet_id)
            # Decide before deleting anything: a refusal must leave the
            # pins as they were.
            fleet_pins = [row_to_pin(r) for r in await conn.fetch(LIST_PINS, fleet_id)]
            refusal = pin_refusal(
                fleet_pins, entry_id, pinned_by, limit, agent_limit, replaces, also_owned_by
            )
            if refusal is not None:
                return refusal
            replaced = [
                old
                for old in dict.fromkeys(replaces)
                if old != entry_id
                and is_valid_uuid(old)
                and (await conn.execute(DELETE_PIN, fleet_id, old)).endswith(" 1")
            ]
            row = await conn.fetchrow(INSERT_PIN, fleet_id, entry_id, pinned_by)
        if row is None:
            raise RuntimeError("pin row vanished between insert and read")
        return PinWrite(row_to_pin(row), bool(row["created"]), tuple(replaced))

    async def unpin_entry(self, fleet_id: str, entry_id: str) -> bool:
        if not is_valid_uuid(entry_id):
            return False
        async with self._connection() as conn:
            status = await conn.execute(DELETE_PIN, fleet_id, entry_id)
        return bool(status.endswith(" 1"))

    async def list_pins(self, fleet_id: str) -> list[Pin]:
        async with self._connection() as conn:
            rows = await conn.fetch(LIST_PINS, fleet_id)
        return [row_to_pin(row) for row in rows]
