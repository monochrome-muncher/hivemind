"""Pin SQL and the pin restore shared by fleet merges and agent cleanups."""

from __future__ import annotations

from collections.abc import Sequence

import asyncpg

from hivemind.domain.fleet_merge import DroppedPin, plan_pin_restore

# ADR 0058: the per-fleet advisory lock makes the pin cap check and the insert atomic.
LOCK_FLEET_PINS = "SELECT pg_advisory_xact_lock(hashtextextended('hivemind.pins:' || $1, 0))"

_PIN_COLUMNS = "fleet_id, entry_id, pinned_by, pinned_at"

_PIN_ORDER = "pinned_at DESC, entry_id"

PINNED_ENTRY_IDS = "SELECT entry_id, pinned_by FROM pins WHERE fleet_id = $1"

RESTORE_PIN = """
INSERT INTO pins (fleet_id, entry_id, pinned_by, pinned_at) VALUES ($1, $2, $3, $4)
ON CONFLICT (fleet_id, entry_id) DO NOTHING
"""


async def restore_fleet_pins(
    conn: asyncpg.Connection, fleet_id: str, pins: Sequence[DroppedPin], home_sql: str
) -> tuple[list[DroppedPin], list[DroppedPin]]:
    """Re-pin where ``home_sql`` (entry ids $1, fleet $2) finds the entry, it is unpinned
    and the cap has room; returns ``(restored, skipped)``."""
    home = {str(r["id"]) for r in await conn.fetch(home_sql, [d.entry_id for d in pins], fleet_id)}
    held = await conn.fetch(PINNED_ENTRY_IDS, fleet_id)
    pinned = {str(r["entry_id"]) for r in held}
    fits = [d for d in pins if d.entry_id in home and d.entry_id not in pinned]
    skipped = [d for d in pins if d not in fits]
    put, over = plan_pin_restore(fits, [r["pinned_by"] for r in held])
    skipped.extend(over)
    for d in put:
        await conn.execute(RESTORE_PIN, fleet_id, d.entry_id, d.pinned_by, d.pinned_at)
    return list(put), skipped
