"""Usage counters, fleet health metrics and the MetricsOut document."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class EntriesMetrics(BaseModel):
    """Entry usage counters (ROADMAP §3.3)."""

    total: int
    active: int
    inactive: int
    by_scope: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    # ROADMAP §4.5.
    by_importance_source: dict[str, int] = {}
    # Author -> kind counts (ROADMAP §4.5), keyed by the agent roster, so
    # an author with no entries maps to {}; zero counts are omitted.
    by_author_kind: dict[str, dict[str, int]] = {}


class FleetPhaseMetrics(BaseModel):
    """A fleet phase in force (ADR 0143)."""

    phase: str
    until: datetime


class FleetsMetrics(BaseModel):
    """Fleet usage counters (writes per fleet, the §12 counter) and, per fleet name, the phase in
    force (ADR 0143)."""

    total: int
    writes_by_fleet: dict[str, int] = {}
    phase_by_fleet: dict[str, FleetPhaseMetrics] = {}


class AgentsMetrics(BaseModel):
    """Agent usage counters (the §12 counters: trust-level distribution,
    pending-agent count)."""

    total: int
    pending: int
    active: int
    revoked: int = 0
    by_trust_level: dict[str, int] = {}
    by_role: dict[str, int] = {}


class FleetSearchMetrics(BaseModel):
    """Search counters for one fleet name."""

    total: int
    empty: int
    explore: int = 0


class SearchesMetrics(BaseModel):
    """Search counters in total and per fleet name (ADRs 0056, 0078).
    Searches from callers without a home fleet count in the totals only."""

    total: int
    empty: int
    explore: int = 0
    by_fleet: dict[str, FleetSearchMetrics] = {}


class FeedbackMetrics(BaseModel):
    """Flag counters: ``stale``/``wrong`` reports made while the
    entry was current, and how many were later confirmed by a supersession.
    The totals include every non-revoked reporter (the admin key too);
    ``by_agent`` is keyed by the registered roster and omits agents with no
    flags. Observation only: never an input to ranking."""

    flags: int = 0
    confirmed: int = 0
    by_agent: dict[str, dict[str, int]] = {}


class Share(BaseModel):
    """``count`` of ``total`` and their ratio (``None`` while ``total`` is 0)."""

    count: int = 0
    total: int = 0
    share: float | None = None


class AuthorShare(Share):
    authors: int = 0


class ReporterShare(Share):
    reporters: int = 0


class OwnerShare(Share):
    owners: int = 0


class SupersessionRate(BaseModel):
    supersessions: int = 0
    writes: int = 0
    per_100: float | None = None


class RivalAge(BaseModel):
    count: int = 0
    median_age_seconds: float | None = None


class RivalLifetime(BaseModel):
    """Active ``alternative`` entries, ``Conflict:`` entries and contested
    entries."""

    alternative: RivalAge = RivalAge()
    conflict: RivalAge = RivalAge()
    contested: RivalAge = RivalAge()


class LabelShare(Share):
    labels: int = 0


class TopicDispersion(BaseModel):
    """Mean pairwise cosine of a busy tag's entries, last 30 days against
    the 30 before; a positive ``change`` means converging (ADR 0075)."""

    tag: str
    recent_entries: int
    before_entries: int
    recent_cosine: float
    before_cosine: float
    change: float


class FleetHealthMetrics(BaseModel):
    """The per-fleet part of the health report (ADR 0075), keyed by entries'
    ``fleet_id``."""

    sourced_share: Share = Share()
    flagged_backlog_14d: int = 0
    orphaned_flagged: int = 0
    floating_gap: int = 0
    gap_tag_counts: dict[str, int] = {}


class RoleActivityMetrics(BaseModel):
    """What scouts, or everyone else, did in the last 30 days (ADR 0144)."""

    writes: int = 0
    alternative_or_dead_end_writes: int = 0
    supersessions: int = 0
    verdicts: int = 0


class HealthMetrics(BaseModel):
    """Fleet health metrics (ADR 0075): admin-only and observation-only,
    never read by search, ranking, scoring, quality or the briefing. The
    windows (90 days for the concentration and supersession figures, 14 and
    30 days for the backlog and orphan ages) are fixed by the ADR.
    Author withdrawals within an hour of the write are not measured: the
    schema records neither when nor by whom an author withdrew."""

    helpful_with_note_share: Share = Share()
    sourced_share: Share = Share()
    correction_latency_p50_seconds: float | None = None
    corrections_measured: int = 0
    flagged_backlog_14d: int = 0
    orphaned_flagged: int = 0
    floating_gap: int = 0
    top3_author_write_share: AuthorShare = AuthorShare()
    top3_reporter_verdict_share: ReporterShare = ReporterShare()
    sibling_verdict_share: Share = Share()
    top3_owner_verdict_share: OwnerShare = OwnerShare()
    synthesis_rate: Share = Share()
    self_to_fleet_promotions: int = 0
    gap_tag_counts: dict[str, int] = {}
    supersessions_per_100_writes: SupersessionRate = SupersessionRate()
    echo_rate: Share = Share()
    topic_dispersion: list[TopicDispersion] = []
    rival_lifetime: RivalLifetime = RivalLifetime()
    grounding_share: Share = Share()
    top3_agent_label_write_share: LabelShare = LabelShare()
    top3_agent_label_verdict_share: LabelShare = LabelShare()
    by_fleet: dict[str, FleetHealthMetrics] = {}
    role_activity_30d: dict[str, RoleActivityMetrics] = {}


class MetricsOut(BaseModel):
    """The minimal usage-counters report (ROADMAP §3.3, admin-gated)."""

    entries: EntriesMetrics
    fleets: FleetsMetrics
    agents: AgentsMetrics
    searches: SearchesMetrics
    feedback: FeedbackMetrics = FeedbackMetrics()
    health: HealthMetrics | None = None
