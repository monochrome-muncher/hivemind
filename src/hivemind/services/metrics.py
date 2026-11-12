"""The usage-counters report (ROADMAP §3.3): entries, fleets, agents, searches (ADRs 0056, 0078),
feedback (ADR 0068) and fleet health (ADR 0075).

The grouped scan of ``entries`` is the one expensive read, cached for ``entry_counts_ttl`` seconds
(single-flight) and shared by ``GET /v1/metrics`` and the Prometheus gauges, so entry and per-fleet
write counts can lag by the TTL. Observation only: nothing in ranking reads these.
"""

from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime

from hivemind.domain import usage
from hivemind.domain.access import (
    SCOPE_FLEET,
    SCOPE_ORG,
    SCOPE_SELF,
    Agent,
    AgentStatus,
    Fleet,
    TrustLevel,
)
from hivemind.domain.entry import ImportanceSource, Kind, SearchCount, UsageCount
from hivemind.domain.feedback import FlagCount
from hivemind.domain.health import Convergence, FleetHealth, HealthCounts
from hivemind.ports import Store

logger = logging.getLogger(__name__)

_SCOPES = (SCOPE_ORG, SCOPE_FLEET, SCOPE_SELF)
_KINDS = (Kind.FACT, Kind.INSIGHT, Kind.DECISION)
_IMPORTANCE_SOURCES = (ImportanceSource.CALLER, ImportanceSource.DEFAULT)
DEFAULT_ENTRY_COUNTS_TTL = 300.0
_TRUST_LEVELS = (
    TrustLevel.UNTRUSTED,
    TrustLevel.LURKER,
    TrustLevel.CONTRIBUTOR,
    TrustLevel.PRIVILEGED,
)
_HEALTH_FIELDS = (
    "sourceable",
    "sourced",
    "insights",
    "synthesizing",
    "flagged_backlog_14d",
    "orphaned_flagged",
    "floating_gap",
    "gap_anomaly",
    "gap_open_question",
    "gap_dead_end",
    "writes_7d",
    "verdicts_7d",
    "supersessions_7d",
    "withdrawals_7d",
)


def _share(count: int, total: int) -> usage.Share:
    """The share; ``None`` while the total is 0."""
    return usage.Share(count=count, total=total, share=count / total if total else None)


def _ages(ages: Sequence[float]) -> usage.Ages:
    """Count and median age in seconds (``None`` while there are none)."""
    return usage.Ages(count=len(ages), median_age_seconds=statistics.median(ages) if ages else None)


def _gap_tags(counts: dict[str, int]) -> dict[str, int]:
    return {
        "anomaly": counts["gap_anomaly"],
        "open-question": counts["gap_open_question"],
        "dead-end": counts["gap_dead_end"],
    }


class MetricsService:
    """Compute the usage report from the ``Store``. ``entry_counts_ttl`` (seconds, 0 = every report)
    also covers the fleet health aggregates; a failed scan is never cached; ``wall_clock`` is "now"
    for health windows."""

    def __init__(
        self,
        store: Store,
        *,
        entry_counts_ttl: float = DEFAULT_ENTRY_COUNTS_TTL,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._ttl = entry_counts_ttl
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = asyncio.Lock()
        self._rows: Sequence[UsageCount] = ()
        self._rows_at: float | None = None
        # Health is cached on its own (ADR 0075): a failure serves the last good values, is retried
        # on the next report and never fails the rest of the report.
        self._health: FleetHealth | None = None
        self._convergence: Convergence | None = None
        self._health_at: float | None = None
        self._health_ok = False
        # Bumped by invalidate(): a scan that started before it must not cache its stale result.
        self._generation = 0

    def invalidate(self) -> None:
        """Drop the cached entry scan and fleet health (after a fleet merge or undo)."""
        self._generation += 1
        self._rows_at = None
        self._health_at = None

    async def _usage_rows(self) -> Sequence[UsageCount]:
        """The grouped entry scan, reused within the TTL; one scan at a time. A store error
        propagates and leaves the previous rows' age untouched."""
        async with self._lock:
            now = self._clock()
            if self._rows_at is not None and now - self._rows_at < self._ttl:
                return self._rows
            generation = self._generation
            rows = await self._store.usage_counts()
            if generation != self._generation:
                return rows  # invalidated mid-scan: serve it, do not cache it
            self._rows = rows
            self._rows_at = now
            return rows

    async def _health_snapshot(self) -> tuple[FleetHealth | None, Convergence | None, bool]:
        """The fleet health aggregates, reused within the TTL, and whether the last refresh
        succeeded. Each read fails on its own: its last good value is kept and retried."""
        async with self._lock:
            now = self._clock()
            if self._health_at is not None and now - self._health_at < self._ttl:
                return self._health, self._convergence, self._health_ok
            wall = self._wall_clock()
            generation = self._generation
            health, convergence = self._health, self._convergence
            ok = True
            try:
                health = await self._store.fleet_health(wall)
            except Exception:  # health must not take the usage report down
                logger.warning("fleet health aggregates failed", exc_info=True)
                ok = False
            try:
                convergence = await self._store.fleet_convergence(wall)
            except Exception:
                logger.warning("fleet convergence figures failed", exc_info=True)
                ok = False
            if generation != self._generation:
                return health, convergence, ok  # invalidated mid-scan: do not cache
            self._health, self._convergence = health, convergence
            self._health_ok = ok
            if ok:
                self._health_at = now
            return health, convergence, ok

    async def usage_report(self) -> usage.UsageReport:
        """The full usage report: a fixed handful of queries however large the pool."""
        agents = await self._store.list_agents()
        # A merged fleet holds nothing (ADR 0107).
        fleets = [f for f in await self._store.list_fleets() if f.merged_into is None]
        rows = await self._usage_rows()
        health, convergence, health_ok = await self._health_snapshot()
        searches = await self._store.search_counts()
        flags = await self._store.flag_counts()
        return usage.UsageReport(
            entries=self._entries_report(agents, rows),
            fleets=self._fleets_report(fleets, rows),
            agents=self._agents_report(agents),
            searches=self._searches_report(fleets, searches),
            feedback=self._feedback_report(agents, flags),
            health=(
                self._health_report(fleets, health, convergence or Convergence())
                if health is not None
                else None
            ),
            health_status=usage.HealthStatus(ok=health_ok),
        )

    def _entries_report(
        self, agents: Sequence[Agent], rows: Sequence[UsageCount]
    ) -> usage.EntriesUsage:
        """Entry counters (ROADMAP §4.5). ``by_author_kind`` is keyed by the registered roster:
        an author with no entries maps to {}, an unregistered author is left out. Zero counts are
        omitted."""
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
        return usage.EntriesUsage(
            total=total,
            active=active,
            inactive=total - active,
            by_scope={s: scope_n[s] for s in _SCOPES if scope_n[s]},
            by_kind={k.value: kind_n[k] for k in _KINDS if kind_n[k]},
            by_importance_source={
                src.value: source_n[src] for src in _IMPORTANCE_SOURCES if source_n[src]
            },
            by_author_kind={
                agent.name: {
                    k.value: author_kind_n[(agent.name, k)]
                    for k in _KINDS
                    if author_kind_n[(agent.name, k)]
                }
                for agent in agents
            },
        )

    def _fleets_report(
        self, fleets: Sequence[Fleet], rows: Sequence[UsageCount]
    ) -> usage.FleetsUsage:
        """Fleet counters: total and writes per fleet."""
        writes: Counter[str] = Counter()
        for r in rows:
            if r.fleet_id is not None:
                writes[r.fleet_id] += r.count
        return usage.FleetsUsage(
            total=len(fleets),
            writes_by_fleet={fleet.name: writes[fleet.id] for fleet in fleets},
        )

    def _searches_report(
        self, fleets: Sequence[Fleet], rows: Sequence[SearchCount]
    ) -> usage.SearchesUsage:
        """Search counters (ADR 0056), in total and per fleet name; callers without a home fleet
        count in the totals only."""
        by_id = {r.fleet_id: r for r in rows}
        return usage.SearchesUsage(
            total=sum(r.searches for r in rows),
            empty=sum(r.empty for r in rows),
            explore=sum(r.explore for r in rows),
            by_fleet={
                fleet.name: usage.FleetSearchCounts(
                    total=row.searches, empty=row.empty, explore=row.explore
                )
                for fleet in fleets
                if (row := by_id.get(fleet.id)) is not None
            },
        )

    def _feedback_report(
        self, agents: Sequence[Agent], rows: Sequence[FlagCount]
    ) -> usage.FeedbackUsage:
        """Flag counters (ADR 0068). Totals cover every reporter that is not revoked, ``admin``
        included; ``by_agent`` uses the registered roster only, to keep labels bounded."""
        by_user = {r.user: r for r in rows}
        return usage.FeedbackUsage(
            flags=sum(r.flags for r in rows),
            confirmed=sum(r.confirmed for r in rows),
            by_agent={
                agent.name: usage.AgentFlagCounts(flags=row.flags, confirmed=row.confirmed)
                for agent in agents
                if (row := by_user.get(agent.name)) is not None
            },
        )

    def _health_report(
        self, fleets: Sequence[Fleet], health: FleetHealth, convergence: Convergence
    ) -> usage.HealthUsage:
        """Fleet health metrics (ADR 0075). A share is ``None`` while its denominator is 0;
        entries with no fleet count in the fleet-wide numbers only."""
        total = {field: sum(getattr(r, field) for r in health.by_fleet) for field in _HEALTH_FIELDS}
        latencies = health.correction_latencies
        by_id = {r.fleet_id: r for r in health.by_fleet}
        return usage.HealthUsage(
            helpful_with_note_share=_share(health.helpful_with_note, health.helpful),
            sourced_share=_share(total["sourced"], total["sourceable"]),
            correction_latency_p50_seconds=(statistics.median(latencies) if latencies else None),
            corrections_measured=len(latencies),
            flagged_backlog_14d=total["flagged_backlog_14d"],
            orphaned_flagged=total["orphaned_flagged"],
            floating_gap=total["floating_gap"],
            top3_author_write_share=usage.Top3Authors(
                **asdict(_share(health.top3_author_writes_90d, health.writes_90d)),
                authors=health.authors_90d,
            ),
            top3_reporter_verdict_share=usage.Top3Reporters(
                **asdict(_share(health.top3_reporter_verdicts_90d, health.verdicts_90d)),
                reporters=health.reporters_90d,
            ),
            sibling_verdict_share=_share(health.sibling_verdicts_90d, health.verdicts_90d),
            top3_owner_verdict_share=usage.Top3Owners(
                **asdict(_share(health.top3_owner_verdicts_90d, health.verdicts_90d)),
                owners=health.owners_90d,
            ),
            synthesis_rate=_share(total["synthesizing"], total["insights"]),
            self_to_fleet_promotions=health.self_to_fleet_promotions_90d,
            gap_tag_counts=_gap_tags(total),
            supersessions_per_100_writes=usage.SupersessionRate(
                supersessions=health.supersessions_90d,
                writes=health.writes_90d,
                per_100=(
                    100 * health.supersessions_90d / health.writes_90d
                    if health.writes_90d
                    else None
                ),
            ),
            echo_rate=_share(convergence.echo_hits, convergence.echo_sampled),
            topic_dispersion=[
                usage.TopicDispersion(
                    tag=d.tag,
                    recent_entries=d.recent_entries,
                    before_entries=d.before_entries,
                    recent_cosine=d.recent_cosine,
                    before_cosine=d.before_cosine,
                    change=d.recent_cosine - d.before_cosine,
                )
                for d in convergence.dispersion
            ],
            rival_lifetime=usage.RivalLifetime(
                alternative=_ages(health.alternative_ages),
                conflict=_ages(health.conflict_ages),
                contested=_ages(health.contested_ages),
            ),
            grounding_share=_share(health.grounded_30d, health.synthesis_writes_30d),
            top3_agent_label_write_share=usage.Top3Labels(
                **asdict(_share(health.top3_label_writes_30d, health.writes_30d)),
                labels=health.write_labels_30d,
            ),
            top3_agent_label_verdict_share=usage.Top3Labels(
                **asdict(_share(health.top3_label_verdicts_30d, health.verdicts_30d)),
                labels=health.verdict_labels_30d,
            ),
            breathing_7d=usage.Breathing(
                writes=total["writes_7d"],
                verdicts=total["verdicts_7d"],
                supersessions=total["supersessions_7d"],
                withdrawals=total["withdrawals_7d"],
            ),
            by_fleet={
                fleet.name: self._fleet_health(by_id.get(fleet.id) or HealthCounts(fleet.id))
                for fleet in fleets
            },
        )

    @staticmethod
    def _fleet_health(row: HealthCounts) -> usage.FleetHealthUsage:
        return usage.FleetHealthUsage(
            active_entries=row.active_entries,
            breathing_7d=usage.Breathing(
                writes=row.writes_7d,
                verdicts=row.verdicts_7d,
                supersessions=row.supersessions_7d,
                withdrawals=row.withdrawals_7d,
            ),
            sourced_share=_share(row.sourced, row.sourceable),
            flagged_backlog_14d=row.flagged_backlog_14d,
            orphaned_flagged=row.orphaned_flagged,
            floating_gap=row.floating_gap,
            gap_tag_counts=_gap_tags(
                {
                    "gap_anomaly": row.gap_anomaly,
                    "gap_open_question": row.gap_open_question,
                    "gap_dead_end": row.gap_dead_end,
                }
            ),
        )

    def _agents_report(self, agents: Sequence[Agent]) -> usage.AgentsUsage:
        """Agent counters: status totals and the trust-level distribution."""
        pending = sum(1 for agent in agents if agent.status is AgentStatus.PENDING)
        active = sum(1 for agent in agents if agent.status is AgentStatus.ACTIVE)
        revoked = sum(1 for agent in agents if agent.status is AgentStatus.REVOKED)
        by_trust_level: dict[str, int] = {}
        for level in _TRUST_LEVELS:
            count = sum(1 for agent in agents if agent.trust_level is level)
            by_trust_level[level.label] = count
        return usage.AgentsUsage(
            total=len(agents),
            pending=pending,
            active=active,
            revoked=revoked,
            by_trust_level=by_trust_level,
        )
