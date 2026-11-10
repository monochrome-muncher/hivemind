"""The fleet guards every fleet write runs under: merged-fleet refusal and stale home."""

from __future__ import annotations

import asyncpg

from hivemind.domain.access import FleetBusy, FleetMerged
from hivemind.domain.validation import is_valid_uuid

AGENT_HOME = "SELECT home_fleet_id FROM agents WHERE name = $1"

WRITE_LOCK_TIMEOUT = "3s"  # how long a fleet write waits for a merge before a 503

GUARD_FLEET = """
SELECT f.merged_into, t.name AS target_name
  FROM fleets f LEFT JOIN fleets t ON t.id = f.merged_into
 WHERE f.id = $1 FOR SHARE OF f
"""


async def refuse_merged(conn: asyncpg.Connection, fleet_id: str | None) -> None:
    """Share-lock ``fleet_id``'s row until the transaction ends; ``FleetMerged`` if merged
    (ADR 0107). An id naming no fleet passes (``entries.fleet_id`` has no FK)."""
    if fleet_id is None or not is_valid_uuid(fleet_id):
        return
    # A merge holds the row while it runs: give up with a retryable answer, not a held connection.
    await conn.execute(f"SET LOCAL lock_timeout = '{WRITE_LOCK_TIMEOUT}'")
    try:
        row = await conn.fetchrow(GUARD_FLEET, fleet_id)
    except asyncpg.exceptions.LockNotAvailableError:
        raise FleetBusy(fleet_id) from None
    if row is not None and row["merged_into"] is not None:
        raise FleetMerged(fleet_id, str(row["merged_into"]), row["target_name"])


async def refuse_stale_home(conn: asyncpg.Connection, fleet_id: str | None, author: str) -> None:
    """ADR 0112: a fleet write by ``author`` that resolved its home before a move committed
    must not land in the old home (``FleetBusy``). Call after the fleet's share lock;
    an author that is no registered agent is not checked."""
    if fleet_id is None:
        return
    home = await conn.fetchrow(AGENT_HOME, author)
    if (
        home is not None
        and home["home_fleet_id"] is not None
        and str(home["home_fleet_id"]) != fleet_id
    ):
        raise FleetBusy(
            fleet_id,
            f"the home fleet of {author} changed during this write, retry in a few seconds",
        )
