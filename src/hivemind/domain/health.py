"""The raw aggregates behind the fleet health metrics (ADR 0075).

Counts only, as the ``Store`` returns them; ``MetricsService`` turns them
into shares and medians. Observation only: nothing in search, ranking,
scoring, quality or the briefing reads these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# A source ref that only names another entry (a bare id with or without
# hyphens, ``entry:<id>``, or an API URL ``.../entries/<id>``): not an
# external source. ``sources`` has no entry type (ADR 0057), so this is how a
# cited entry is recognised. One pattern for the ``no_primary_source`` write
# warning and the grounding metric, valid in Python and Postgres regexes
# (both match it case-insensitively).
ENTRY_ID_REF = (
    r"^\s*(entry:|.*/entries/)?"
    r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}/?\s*$"
)
_ENTRY_ID_REF = re.compile(ENTRY_ID_REF, re.IGNORECASE)


def is_entry_ref(ref: str) -> bool:
    """Whether a source ``ref`` only names another entry."""
    return _ENTRY_ID_REF.match(ref) is not None


@dataclass(frozen=True, slots=True)
class HealthCounts:
    """Additive counters over the active entries of one fleet
    (``fleet_id`` ``None``: entries with no fleet). Fleet-wide numbers are
    the sum over all rows.

    ``sourceable`` are active org- and fleet-scope facts and insights and
    ``sourced`` those with at least one source; ``insights`` are active
    insights and ``synthesizing`` those linking at least two entries
    (``see_also``). ``flagged_backlog_14d`` counts active entries whose
    oldest standing stale/wrong verdict is over 14 days old,
    ``orphaned_flagged`` flagged ones whose author is revoked or has
    written nothing for 30 days, ``floating_gap`` active entries whose
    ``occurred_at`` and last feedback are both over 90 days old. The
    ``gap_*`` fields count active entries carrying that tag.
    """

    fleet_id: str | None
    sourceable: int = 0
    sourced: int = 0
    insights: int = 0
    synthesizing: int = 0
    flagged_backlog_14d: int = 0
    orphaned_flagged: int = 0
    floating_gap: int = 0
    gap_anomaly: int = 0
    gap_open_question: int = 0
    gap_dead_end: int = 0


@dataclass(frozen=True, slots=True)
class FleetHealth:
    """Everything ``Store.fleet_health`` measures at one instant.

    The 90-day figures cover entries created (or verdicts last written) in
    the 90 days before ``now``. ``top3_*`` are the sums of the three
    largest per-author / per-reporter counts. ``correction_latencies`` are
    seconds from an entry's first standing stale/wrong verdict to its
    successor's creation, for successors created in those 90 days.
    """

    by_fleet: tuple[HealthCounts, ...] = ()
    helpful: int = 0
    helpful_with_note: int = 0
    correction_latencies: tuple[float, ...] = ()
    writes_90d: int = 0
    top3_author_writes_90d: int = 0
    authors_90d: int = 0
    verdicts_90d: int = 0
    top3_reporter_verdicts_90d: int = 0
    reporters_90d: int = 0
    supersessions_90d: int = 0
    self_to_fleet_promotions_90d: int = 0
    # Convergence figures (ADR 0075). Ages are seconds since ``created_at``
    # of the active entries tagged ``alternative``, whose summary starts
    # "Conflict:", and that hold both a helpful and a stale/wrong verdict.
    alternative_ages: tuple[float, ...] = ()
    conflict_ages: tuple[float, ...] = ()
    contested_ages: tuple[float, ...] = ()
    # Grounding: fleet/org insights and decisions written in the last 30
    # days, and those with a source that is not an entry id.
    synthesis_writes_30d: int = 0
    grounded_30d: int = 0
    # Writes / verdicts of the last 30 days by self-reported ``agent`` label.
    writes_30d: int = 0
    top3_label_writes_30d: int = 0
    write_labels_30d: int = 0
    verdicts_30d: int = 0
    top3_label_verdicts_30d: int = 0
    verdict_labels_30d: int = 0


# Bounds of the vector work (ADR 0075): how many of the newest writes the
# echo rate samples, the similarity that makes a write an echo, how many
# busiest tags the dispersion covers, the entries per tag and window, and
# the entries a tag needs in each window to be reported.
ECHO_SAMPLE = 200
ECHO_THRESHOLD = 0.9
DISPERSION_TAGS = 10
DISPERSION_CAP = 50
DISPERSION_MIN = 5


@dataclass(frozen=True, slots=True)
class TagDispersion:
    """Mean pairwise cosine of the (up to ``DISPERSION_CAP`` newest) entries
    carrying ``tag``, written in the last 30 days and in the 30 days
    before. A rise means the writers are converging."""

    tag: str
    recent_entries: int
    before_entries: int
    recent_cosine: float
    before_cosine: float


@dataclass(frozen=True, slots=True)
class Convergence:
    """The vector-backed convergence figures (ADR 0075): of the
    ``echo_sampled`` newest writes of the last 30 days, ``echo_hits`` had an
    earlier active entry at cosine >= ``ECHO_THRESHOLD``; and the busiest
    tags' dispersion, busiest first."""

    echo_sampled: int = 0
    echo_hits: int = 0
    dispersion: tuple[TagDispersion, ...] = ()
