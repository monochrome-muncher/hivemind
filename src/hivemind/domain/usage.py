"""The typed usage report (ADR 0084): the shape of
``MetricsService.usage_report()``.

One dataclass per report section; ``metrics.py`` owns the values and the
cache, this module owns the shape. A renamed field is a type error at the
service/route/prometheus seam, not a 500 at scrape time.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Share:
    """``{count, total, share}``; the share is ``None`` while the total is 0."""

    count: int
    total: int
    share: float | None


@dataclass(frozen=True, slots=True)
class Ages:
    """A count and the median age in seconds (``None`` while there are none)."""

    count: int
    median_age_seconds: float | None


@dataclass(frozen=True, slots=True)
class TopicDispersion:
    """One tag's dispersion between its recent and older entries (ADR 0075)."""

    tag: str
    recent_entries: int
    before_entries: int
    recent_cosine: float
    before_cosine: float
    change: float


@dataclass(frozen=True, slots=True)
class Top3Authors:
    """Top-3 author writes over the window, with the roster size."""

    count: int
    total: int
    share: float | None
    authors: int


@dataclass(frozen=True, slots=True)
class Top3Reporters:
    """Top-3 reporter verdicts over the window, with the roster size."""

    count: int
    total: int
    share: float | None
    reporters: int


@dataclass(frozen=True, slots=True)
class Top3Labels:
    """Top-3 agent-label writes/verdicts over the window, with the labels."""

    count: int
    total: int
    share: float | None
    labels: int


@dataclass(frozen=True, slots=True)
class SupersessionRate:
    """Supersessions per 100 writes over the window (``per_100`` is
    ``None`` while there are no writes)."""

    supersessions: int
    writes: int
    per_100: float | None


@dataclass(frozen=True, slots=True)
class RivalLifetime:
    """Age distributions of alternative / conflict / contested entries."""

    alternative: Ages
    conflict: Ages
    contested: Ages


@dataclass(frozen=True, slots=True)
class FleetHealthUsage:
    """One fleet's health slice (``by_fleet`` lists every fleet)."""

    sourced_share: Share
    flagged_backlog_14d: int
    orphaned_flagged: int
    floating_gap: int
    gap_tag_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class EntriesUsage:
    """Entry usage counters (ROADMAP §3.3, §4.5). Zero counts are omitted;
    keys follow vocabulary order."""

    total: int
    active: int
    inactive: int
    by_scope: dict[str, int]
    by_kind: dict[str, int]
    by_importance_source: dict[str, int]
    by_author_kind: dict[str, dict[str, int]]


@dataclass(frozen=True, slots=True)
class FleetsUsage:
    """Fleet usage counters (writes per fleet, the §12 counter)."""

    total: int
    writes_by_fleet: dict[str, int]


@dataclass(frozen=True, slots=True)
class AgentsUsage:
    """Agent usage counters (the §12 counters: trust-level distribution,
    pending-agent count, ADR 0012)."""

    total: int
    pending: int
    active: int
    revoked: int
    by_trust_level: dict[str, int]


@dataclass(frozen=True, slots=True)
class FleetSearchCounts:
    """Search counters for one fleet name (ADR 0056)."""

    total: int
    empty: int
    explore: int


@dataclass(frozen=True, slots=True)
class SearchesUsage:
    """Search counters in total and per fleet name (ADRs 0056, 0078).
    Searches from callers without a home fleet count in the totals only."""

    total: int
    empty: int
    explore: int
    by_fleet: dict[str, FleetSearchCounts]


@dataclass(frozen=True, slots=True)
class AgentFlagCounts:
    """One reporter's flag counters (ADR 0068)."""

    flags: int
    confirmed: int


@dataclass(frozen=True, slots=True)
class FeedbackUsage:
    """Flag counters (ADR 0068). The totals cover every reporter that is
    not revoked, the admin key included; ``by_agent`` is keyed by the
    registered roster only and leaves out agents with no flags."""

    flags: int
    confirmed: int
    by_agent: dict[str, AgentFlagCounts]


@dataclass(frozen=True, slots=True)
class HealthUsage:
    """Fleet health metrics (ADR 0075). A share is ``None`` while its
    denominator is 0; ``by_fleet`` lists every fleet (zeros included)."""

    helpful_with_note_share: Share
    sourced_share: Share
    correction_latency_p50_seconds: float | None
    corrections_measured: int
    flagged_backlog_14d: int
    orphaned_flagged: int
    floating_gap: int
    top3_author_write_share: Top3Authors
    top3_reporter_verdict_share: Top3Reporters
    synthesis_rate: Share
    self_to_fleet_promotions: int
    gap_tag_counts: dict[str, int]
    supersessions_per_100_writes: SupersessionRate
    echo_rate: Share
    topic_dispersion: list[TopicDispersion]
    rival_lifetime: RivalLifetime
    grounding_share: Share
    top3_agent_label_write_share: Top3Labels
    top3_agent_label_verdict_share: Top3Labels
    by_fleet: dict[str, FleetHealthUsage]


@dataclass(frozen=True, slots=True)
class HealthStatus:
    """Whether the health snapshot was fully available (a degraded scrape
    reports ``ok=False``; the usage sections still answer)."""

    ok: bool


@dataclass(frozen=True, slots=True)
class UsageReport:
    """The full usage report: one section per dataclass."""

    entries: EntriesUsage
    fleets: FleetsUsage
    agents: AgentsUsage
    searches: SearchesUsage
    feedback: FeedbackUsage
    health: HealthUsage | None
    health_status: HealthStatus
