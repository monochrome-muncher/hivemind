"""The admin activity view (ADR 0080): who wrote, who reported, per day.

A read-only report folded from ``Store.activity`` plus the agent and fleet
rosters, served at ``GET /v1/admin/activity``. The grouped scans are the
expensive read, so the service caches them per ``days`` value and UTC date for ``ttl``
seconds (single-flight; the ``metrics_entry_counts_ttl`` setting, no new
variable); the rosters are small and read on every call. A failed read is
never cached.

Observation only, like the feedback counters (ADR 0068) and the fleet
health metrics (ADR 0075): nothing in search, ranking, scoring, quality
or the briefing reads it, and no agent-facing surface exposes it.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from hivemind.domain.access import AgentStatus
from hivemind.domain.activity import Activity
from hivemind.ports import Store

MIN_DAYS = 1
MAX_DAYS = 365
DEFAULT_DAYS = 30


def window_start(now: datetime, days: int) -> datetime:
    """UTC midnight of the first of the ``days`` days ending today."""
    today = now.astimezone(UTC).date()
    first = today - timedelta(days=days - 1)
    return datetime(first.year, first.month, first.day, tzinfo=UTC)


def _later(a: datetime | None, b: datetime | None) -> datetime | None:
    if a is None or b is None:
        return a or b
    return max(a, b)


class ActivityService:
    """Compute the activity report from the ``Store`` (ADR 0080)."""

    def __init__(
        self,
        store: Store,
        *,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._ttl = ttl
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = asyncio.Lock()
        self._cache: dict[tuple[int, date], tuple[float, datetime, Activity]] = {}

    def invalidate(self) -> None:
        """Drop the cached scans, so the next report scans again (after a
        fleet merge or undo moved the entries)."""
        self._cache.clear()

    async def _activity(self, days: int) -> tuple[datetime, Activity]:
        """The grouped scans for ``days``, reused within the TTL. One scan
        at a time; concurrent callers wait and reuse its result. A store
        error propagates and caches nothing."""
        async with self._lock:
            now = self._clock()
            wall = self._wall_clock()
            today = wall.astimezone(UTC).date()
            # Keyed by the UTC date, so the window never straddles midnight;
            # entries of other dates are dropped to keep the cache bounded.
            self._cache = {k: v for k, v in self._cache.items() if k[1] == today}
            hit = self._cache.get((days, today))
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1], hit[2]
            since = window_start(wall, days)
            activity = await self._store.activity(since)
            self._cache[(days, today)] = (now, since, activity)
            return since, activity

    async def report(self, days: int = DEFAULT_DAYS) -> dict[str, Any]:
        """The report: zero-filled ``daily``, ``totals``, one row per agent
        name and one per fleet (see ``ActivityOut``)."""
        agents = await self._store.list_agents()
        fleets = [f for f in await self._store.list_fleets() if f.merged_into is None]
        since, activity = await self._activity(days)

        by_day = {d.day: d for d in activity.days}
        daily: list[dict[str, Any]] = []
        day = since.date()
        last = since.date() + timedelta(days=days - 1)
        while day <= last:
            hit = by_day.get(day)
            daily.append(
                {
                    "day": day,
                    "writes": hit.writes if hit else 0,
                    "helpful": hit.helpful if hit else 0,
                    "stale": hit.stale if hit else 0,
                    "wrong": hit.wrong if hit else 0,
                }
            )
            day += timedelta(days=1)

        fleet_name = {f.id: f.name for f in fleets}
        registered = {a.name: a for a in agents}
        written = {a.author for a in activity.authors if a.recent > 0}
        reported = {r.reporter for r in activity.reporters if r.recent > 0}

        rows: dict[str, dict[str, Any]] = {}

        def agent_row(name: str) -> dict[str, Any]:
            if name not in rows:
                agent = registered.get(name)
                rows[name] = {
                    "name": name,
                    "registered": agent is not None,
                    "owner_alias": agent.owner_alias if agent else None,
                    "home_fleet": (
                        fleet_name.get(agent.home_fleet_id)
                        if agent and agent.home_fleet_id
                        else None
                    ),
                    "status": agent.status.value if agent else None,
                    "entries": 0,
                    "active": 0,
                    "recent_writes": 0,
                    "last_write_at": None,
                    "verdicts_given": 0,
                    "recent_verdicts": 0,
                    "last_verdict_at": None,
                    "received": {"helpful": 0, "stale": 0, "wrong": 0},
                }
            return rows[name]

        for name in registered:
            agent_row(name)
        for a in activity.authors:
            r = agent_row(a.author)
            r["entries"] += a.entries
            r["active"] += a.active
            r["recent_writes"] += a.recent
            r["last_write_at"] = _later(r["last_write_at"], a.last_write_at)
        for rep in activity.reporters:
            r = agent_row(rep.reporter)
            r["verdicts_given"] += rep.verdicts
            r["recent_verdicts"] += rep.recent
            r["last_verdict_at"] = _later(r["last_verdict_at"], rep.last_at)
        for rc in activity.received:
            r = agent_row(rc.author)
            r["received"] = {"helpful": rc.helpful, "stale": rc.stale, "wrong": rc.wrong}

        fleet_rows: dict[str | None, dict[str, Any]] = {
            f.id: _fleet_row(f.id, f.name) for f in fleets
        }
        authors_in: dict[str | None, set[str]] = defaultdict(set)
        for a in activity.authors:
            if a.fleet_id is not None and a.fleet_id not in fleet_rows:
                continue  # a fleet the roster no longer lists
            fr = fleet_rows.setdefault(a.fleet_id, _fleet_row(None, None))
            fr["entries"] += a.entries
            fr["active"] += a.active
            fr["recent_writes"] += a.recent
            fr["last_write_at"] = _later(fr["last_write_at"], a.last_write_at)
            if a.entries > 0:
                authors_in[a.fleet_id].add(a.author)
        for fleet_id, names in authors_in.items():
            fleet_rows[fleet_id]["writers"] = len(names)
        for agent in agents:
            if agent.status is AgentStatus.ACTIVE and agent.home_fleet_id in fleet_rows:
                fleet_rows[agent.home_fleet_id]["agents"] += 1

        return {
            "days": days,
            "since": since,
            "daily": daily,
            "totals": {
                "writes": sum(a.recent for a in activity.authors),
                "verdicts": sum(r.recent for r in activity.reporters),
                "writers": len(written),
                "reporters": len(reported),
            },
            "agents": sorted(rows.values(), key=lambda r: (-r["recent_writes"], r["name"])),
            "fleets": list(fleet_rows.values()),
        }


def _fleet_row(fleet_id: str | None, name: str | None) -> dict[str, Any]:
    return {
        "fleet_id": fleet_id,
        "name": name,
        "entries": 0,
        "active": 0,
        "recent_writes": 0,
        "writers": 0,
        "last_write_at": None,
        "agents": 0,
    }
