"""The minimal usage-counters surface (ROADMAP §3.3, Tier 3).

A cheap, read-only metrics service over the ``Store`` seam: it computes a
usage report (entries, fleets, agents) from the store so the SPEC §10
*usage-based* triggers and the §12 counters become measurable rather
than guesswork. It is deliberately minimal — a handful of ``COUNT``
queries (``count_entries``) + the fleet/agent listings — no new
instrumentation logs, no extra schema.

Counters (the §12 set from ROADMAP §3.3, plus the §4.5 provenance counter):
- entries: total, active, by_scope, by_kind, by_importance_source, inactive.
- fleets: total + per-fleet writes (``entries.fleet_id``).
- agents: total, pending, active, trust-level distribution.
"""

from __future__ import annotations

from typing import Any

from hivemind.domain.access import AgentStatus, TrustLevel
from hivemind.domain.entry import EntryFilters, ImportanceSource, Kind
from hivemind.ports import Store

# The canonical scopes / kinds (the "distinct tags in use" signal).
_SCOPES = ("org", "fleet", "self")
_KINDS = (Kind.FACT, Kind.INSIGHT, Kind.DECISION)
_IMPORTANCE_SOURCES = (ImportanceSource.CALLER, ImportanceSource.DEFAULT)
_TRUST_LEVELS = (
    TrustLevel.UNTRUSTED,
    TrustLevel.LURKER,
    TrustLevel.CONTRIBUTOR,
    TrustLevel.PRIVILEGED,
)


class MetricsService:
    """Compute the usage report from the ``Store`` (ROADMAP §3.3)."""

    def __init__(self, store: Store) -> None:
        self._store = store

    async def usage_report(self) -> dict[str, dict[str, Any]]:
        """The full usage report (entries / fleets / agents)."""
        return {
            "entries": await self._entries_report(),
            "fleets": await self._fleets_report(),
            "agents": await self._agents_report(),
        }

    async def _entries_report(self) -> dict[str, Any]:
        """Entry counters: total / active / inactive + by_scope + by_kind +
        by_importance_source (ROADMAP §4.5: is anyone actually setting
        ``importance``, or is every entry riding the default?)."""
        store = self._store
        total = await store.count_entries(EntryFilters(include_inactive=True))
        active = await store.count_entries(EntryFilters())
        by_scope: dict[str, int] = {}
        for scope in _SCOPES:
            count = await store.count_entries(EntryFilters(scope=scope, include_inactive=True))
            if count:
                by_scope[scope] = count
        by_kind: dict[str, int] = {}
        for kind in _KINDS:
            count = await store.count_entries(EntryFilters(kind=kind, include_inactive=True))
            if count:
                by_kind[kind.value] = count
        by_importance_source: dict[str, int] = {}
        for source in _IMPORTANCE_SOURCES:
            count = await store.count_entries(
                EntryFilters(importance_source=source, include_inactive=True)
            )
            if count:
                by_importance_source[source.value] = count
        return {
            "total": total,
            "active": active,
            "inactive": total - active,
            "by_scope": by_scope,
            "by_kind": by_kind,
            "by_importance_source": by_importance_source,
        }

    async def _fleets_report(self) -> dict[str, Any]:
        """Fleet counters: total + writes per fleet (``entries.fleet_id``)."""
        store = self._store
        fleets = await store.list_fleets()
        writes_by_fleet: dict[str, int] = {}
        for fleet in fleets:
            count = await store.count_entries(
                EntryFilters(fleet_id=fleet.id, include_inactive=True)
            )
            writes_by_fleet[fleet.name] = count
        return {"total": len(fleets), "writes_by_fleet": writes_by_fleet}

    async def _agents_report(self) -> dict[str, Any]:
        """Agent counters: total / pending / active + trust-level
        distribution (the §12 counters, ROADMAP §3.3)."""
        agents = await self._store.list_agents()
        pending = sum(1 for agent in agents if agent.status is AgentStatus.PENDING)
        active = sum(1 for agent in agents if agent.status is AgentStatus.ACTIVE)
        by_trust_level: dict[str, int] = {}
        for level in _TRUST_LEVELS:
            count = sum(1 for agent in agents if agent.trust_level is level)
            by_trust_level[level.name.lower()] = count
        return {
            "total": len(agents),
            "pending": pending,
            "active": active,
            "by_trust_level": by_trust_level,
        }
