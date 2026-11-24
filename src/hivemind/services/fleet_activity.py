"""The portal's fleet activity chart (ADR 0165), served at ``GET /v1/portal/fleet-activity``.

Activity only: the agents come back by name, never ranked, and nothing in retrieval reads it. The
answers are cached per key and window for ``ttl`` seconds, because the ratings side scans
``feedbacks`` without an index: one key reloading its home page does not repeat the scan.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from hivemind.domain.access import TrustLevel, Visibility
from hivemind.domain.activity import AgentDayActivity
from hivemind.ports import MetricsPort
from hivemind.services.activity import window_start

CACHE_SIZE = 256

_CacheKey = tuple[Visibility, int, date]


class FleetActivityService:
    """Compute the fleet activity report from the ``Store``."""

    def __init__(
        self,
        store: MetricsPort,
        *,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._ttl = ttl
        self._clock = clock
        self._wall_clock = wall_clock
        self._cache: OrderedDict[_CacheKey, tuple[float, dict[str, Any]]] = OrderedDict()
        # Bumped by invalidate(): a read that started before it must not cache its stale result.
        self._generation = 0

    def invalidate(self) -> None:
        """Drop the cached reports (after a merge, a cleanup, an owner change or a rename)."""
        self._generation += 1
        self._cache.clear()

    async def report(self, visibility: Visibility, days: int) -> dict[str, Any]:
        """One zero-filled row per day of the window for each member of the key's home fleet that
        wrote or rated in it, by name; ``quiet`` counts the active members that did neither
        (``PortalFleetActivityOut``). Without a home fleet, or with a key that can read nothing
        (level 0), there are no agents and nobody is quiet."""
        now = self._wall_clock()
        fleet_id = visibility.home_fleet_id
        since = window_start(now, days)
        if fleet_id is None or (
            visibility.level == TrustLevel.UNTRUSTED and not visibility.is_admin
        ):
            return _report(fleet_id, days, since, 0, [], [])
        key: _CacheKey = (visibility, days, now.date())
        hit = self._cache.get(key)
        if hit is not None and self._clock() - hit[0] < self._ttl:
            return hit[1]
        generation = self._generation
        until = since + timedelta(days=days)
        activity = await self._store.fleet_activity(fleet_id, since, until, visibility)
        window = [since.date() + timedelta(days=i) for i in range(days)]
        report = _report(fleet_id, days, since, activity.quiet, list(activity.days), window)
        if generation == self._generation:
            self._cache[key] = (self._clock(), report)
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return report


def _report(
    fleet_id: str | None,
    days: int,
    since: datetime,
    quiet: int,
    rows: list[AgentDayActivity],
    window: list[date],
) -> dict[str, Any]:
    by_agent: dict[str, dict[date, AgentDayActivity]] = {}
    for row in rows:
        by_agent.setdefault(row.agent, {})[row.day] = row
    return {
        "fleet_id": fleet_id,
        "days": days,
        "since": since,
        "quiet": quiet,
        "agents": [_agent_row(name, by_agent[name], window) for name in sorted(by_agent)],
    }


def _agent_row(name: str, rows: dict[date, AgentDayActivity], window: list[date]) -> dict[str, Any]:
    daily = [
        {
            "day": day,
            "writes": rows[day].writes if day in rows else 0,
            "ratings": rows[day].ratings if day in rows else 0,
        }
        for day in window
    ]
    return {
        "name": name,
        "writes": sum(d["writes"] for d in daily),
        "ratings": sum(d["ratings"] for d in daily),
        "daily": daily,
    }
