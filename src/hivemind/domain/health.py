"""Raw aggregates behind the fleet health metrics (ADR 0075). Observation only: nothing in
search, ranking, scoring, quality or the briefing reads them."""

from __future__ import annotations

import re
from dataclasses import dataclass

# A source ref that only names another entry (bare id, ``entry:<id>`` or ``.../entries/<id>``),
# since ``sources`` has no entry type (ADR 0057). One pattern for the ``no_primary_source``
# warning and the grounding metric, valid in Python and Postgres (matched case-insensitively).
ENTRY_ID_REF = (
    r"^\s*(entry:|.*/entries/)?"
    r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}/?\s*$"
)
_ENTRY_ID_REF = re.compile(ENTRY_ID_REF, re.IGNORECASE)


def is_entry_ref(ref: str) -> bool:
    """Whether a source ``ref`` only names another entry."""
    return _ENTRY_ID_REF.match(ref) is not None


def owner_key(alias: str | None) -> str | None:
    """An owner alias as owner metrics compare it (ADR 0089): ASCII-trimmed, lower-cased, ``None``
    when blank. The Postgres query applies the same rule."""
    return (alias or "").strip(" \t\r\n\f\v").lower() or None


@dataclass(frozen=True, slots=True)
class HealthCounts:
    """Additive counters over one fleet's active entries (``fleet_id`` ``None``: no fleet); sum
    the rows for fleet-wide numbers.

    ``sourceable``: org- and fleet-scope facts and insights, ``sourced`` those with a source;
    ``synthesizing``: insights linking at least two entries. ``flagged_backlog_14d``: oldest
    standing stale/wrong verdict over 14 days old; ``orphaned_flagged``: author revoked or silent
    30 days; ``floating_gap``: ``occurred_at`` and last feedback both over 90 days old.
    ``gap_*`` count entries with that tag.
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
    """Everything ``Store.fleet_health`` measures at one instant. The 90-day figures cover the 90
    days before ``now``; ``top3_*`` sum the three largest per-author/reporter/owner counts;
    ``correction_latencies`` are seconds from an entry's first standing flag to its successor."""

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
    # Owner level (ADR 0089): verdicts between different agents of one owner alias; grouped by
    # reporter's owner (no alias: its own owner).
    sibling_verdicts_90d: int = 0
    top3_owner_verdicts_90d: int = 0
    owners_90d: int = 0
    supersessions_90d: int = 0
    self_to_fleet_promotions_90d: int = 0
    # Convergence (ADR 0075): ages in seconds since ``created_at`` of active entries tagged
    # ``alternative``, summarised "Conflict:", or holding both helpful and stale/wrong verdicts.
    alternative_ages: tuple[float, ...] = ()
    conflict_ages: tuple[float, ...] = ()
    contested_ages: tuple[float, ...] = ()
    # Grounding: fleet/org insights and decisions of 30 days, and those with a non-entry source.
    synthesis_writes_30d: int = 0
    grounded_30d: int = 0
    # 30-day writes and verdicts by self-reported ``agent`` label.
    writes_30d: int = 0
    top3_label_writes_30d: int = 0
    write_labels_30d: int = 0
    verdicts_30d: int = 0
    top3_label_verdicts_30d: int = 0
    verdict_labels_30d: int = 0


# Bounds of the vector work (ADR 0075): echo sample size and similarity threshold, busiest tags
# covered, entries per tag and window, and the minimum per window to report a tag.
ECHO_SAMPLE = 200
ECHO_THRESHOLD = 0.9
DISPERSION_TAGS = 10
DISPERSION_CAP = 50
DISPERSION_MIN = 5


@dataclass(frozen=True, slots=True)
class TagDispersion:
    """Mean pairwise cosine of up to ``DISPERSION_CAP`` newest entries with ``tag``, in the last
    30 days and the 30 before; a rise means writers are converging."""

    tag: str
    recent_entries: int
    before_entries: int
    recent_cosine: float
    before_cosine: float


@dataclass(frozen=True, slots=True)
class Convergence:
    """Vector-backed convergence (ADR 0075): ``echo_hits`` of ``echo_sampled`` recent writes had an
    earlier active entry at cosine >= ``ECHO_THRESHOLD``; plus the busiest tags' dispersion."""

    echo_sampled: int = 0
    echo_hits: int = 0
    dispersion: tuple[TagDispersion, ...] = ()
