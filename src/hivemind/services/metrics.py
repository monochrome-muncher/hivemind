"""The minimal usage-counters surface (ROADMAP §3.3, Tier 3).

A read-only usage report that makes the SPEC §10 usage-based triggers
measurable, from one grouped scan plus the fleet/agent listings.

Counters (ROADMAP §3.3 and §4.5):
- entries: total, active, by_scope, by_kind, by_importance_source,
  by_author_kind, inactive.
- fleets: total + per-fleet writes (``entries.fleet_id``).
- agents: total, pending, active, trust-level distribution.
- searches (ADR 0056): first-page searches and how many found nothing,
  in total and per home fleet of the searching agent.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from hivemind.domain.access import Agent, AgentStatus, Fleet, TrustLevel
from hivemind.domain.entry import ImportanceSource, Kind, SearchCount, UsageCount
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
        """The full usage report (entries / fleets / agents).

        A fixed handful of queries however large the pool: every entry
        counter is folded from one grouped scan (PERF-2).
        """
        agents = await self._store.list_agents()
        fleets = await self._store.list_fleets()
        rows = await self._store.usage_counts()
        searches = await self._store.search_counts()
        return {
            "entries": self._entries_report(agents, rows),
            "fleets": self._fleets_report(fleets, rows),
            "agents": self._agents_report(agents),
            "searches": self._searches_report(fleets, searches),
        }

    def _entries_report(
        self, agents: Sequence[Agent], rows: Sequence[UsageCount]
    ) -> dict[str, Any]:
        """Entry counters (ROADMAP §3.3, §4.5).

        ``by_author_kind`` is keyed by the registered agent roster, so an
        author with no entries maps to {} and an unregistered author is
        left out. Zero counts are omitted; keys follow vocabulary order.
        """
        total = sum(r.count for r in rows)
        active = sum(r.count for r in rows if r.active)
        scope_n: Counter[str] = Counter()
        kind_n: Counter[Kind] = Counter()
        source_n: Counter[ImportanceSource] = Counter()
        author_kind_n: Counter[tuple[str, Kind]] = Counter()
        for r in rows:
            scope_n[r.scope] += r.count
            kind_n[r.kind] += r.count
            source_n[r.importance_source] += r.count
            author_kind_n[(r.author, r.kind)] += r.count
        return {
            "total": total,
            "active": active,
            "inactive": total - active,
            "by_scope": {s: scope_n[s] for s in _SCOPES if scope_n[s]},
            "by_kind": {k.value: kind_n[k] for k in _KINDS if kind_n[k]},
            "by_importance_source": {
                src.value: source_n[src] for src in _IMPORTANCE_SOURCES if source_n[src]
            },
            "by_author_kind": {
                agent.name: {
                    k.value: author_kind_n[(agent.name, k)]
                    for k in _KINDS
                    if author_kind_n[(agent.name, k)]
                }
                for agent in agents
            },
        }

    def _fleets_report(self, fleets: Sequence[Fleet], rows: Sequence[UsageCount]) -> dict[str, Any]:
        """Fleet counters: total + writes per fleet (``entries.fleet_id``)."""
        writes: Counter[str] = Counter()
        for r in rows:
            if r.fleet_id is not None:
                writes[r.fleet_id] += r.count
        return {
            "total": len(fleets),
            "writes_by_fleet": {fleet.name: writes[fleet.id] for fleet in fleets},
        }

    def _searches_report(
        self, fleets: Sequence[Fleet], rows: Sequence[SearchCount]
    ) -> dict[str, Any]:
        """Search counters (ADR 0056): first-page searches and the ones that
        found nothing, in total and per fleet name. Searches from callers
        without a home fleet count in the totals only."""
        by_id = {r.fleet_id: r for r in rows}
        return {
            "total": sum(r.searches for r in rows),
            "empty": sum(r.empty for r in rows),
            "by_fleet": {
                fleet.name: {"total": row.searches, "empty": row.empty}
                for fleet in fleets
                if (row := by_id.get(fleet.id)) is not None
            },
        }

    def _agents_report(self, agents: Sequence[Agent]) -> dict[str, Any]:
        """Agent counters: total / pending / active / revoked + trust-level
        distribution (the §12 counters, ROADMAP §3.3)."""
        pending = sum(1 for agent in agents if agent.status is AgentStatus.PENDING)
        active = sum(1 for agent in agents if agent.status is AgentStatus.ACTIVE)
        revoked = sum(1 for agent in agents if agent.status is AgentStatus.REVOKED)
        by_trust_level: dict[str, int] = {}
        for level in _TRUST_LEVELS:
            count = sum(1 for agent in agents if agent.trust_level is level)
            by_trust_level[level.name.lower()] = count
        return {
            "total": len(agents),
            "pending": pending,
            "active": active,
            "revoked": revoked,
            "by_trust_level": by_trust_level,
        }
