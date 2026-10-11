"""Social-signal scenarios S1-S4, computed through the shipped scoring.

Each scenario is two candidates, scored with ``feedback_quality`` and
``entry_score`` under the shipped ``SearchConfig()``, with importance 3 and
an equal fused score unless the scenario says otherwise. No embeddings, no
store: pure arithmetic on today's formula (SPEC §6.4, ADR 0070).

These are baselines, not a gate. They record who wins today and by how
much, so a later ranking change that flips one does it deliberately: it
edits ``EXPECTED_WINNER`` in the same change and says why in its ADR.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from hivemind.config import SearchConfig
from hivemind.retrieval.rrf import rrf_fuse
from hivemind.retrieval.scoring import entry_score, feedback_quality
from tests.fakes import FIXED_NOW

CONFIG = SearchConfig()


@dataclass(frozen=True, slots=True)
class Candidate:
    """One entry in a scenario: its age and feedback, and the match it arrives with."""

    name: str
    age_days: float
    counts: tuple[int, int, int] = (0, 0, 0)
    fused: float = 1.0
    importance: int = 3
    # Days ago of the entry's "last confirmed" date; reaches the score only
    # under a config with ``recency_from_confirmation`` on (ADR 0071).
    confirmed_ago_days: float | None = None


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    title: str
    first: Candidate
    second: Candidate


def score(candidate: Candidate, config: SearchConfig = CONFIG) -> float:
    """The final score of one candidate under ``config`` (the service's call)."""
    quality = feedback_quality(*candidate.counts, **config.quality_kwargs())
    confirmed_at = (
        FIXED_NOW - timedelta(days=candidate.confirmed_ago_days)
        if config.recency_from_confirmation and candidate.confirmed_ago_days is not None
        else None
    )
    return entry_score(
        candidate.fused,
        candidate.importance,
        FIXED_NOW - timedelta(days=candidate.age_days),
        quality,
        FIXED_NOW,
        config.half_life_days,
        recency_floor=config.recency_floor,
        social_max=config.social_max,
        confirmed_at=confirmed_at,
    )


def winner(scenario: Scenario, config: SearchConfig = CONFIG) -> str:
    """The name of the higher-scoring candidate (the first on an exact tie)."""
    a, b = score(scenario.first, config), score(scenario.second, config)
    return scenario.first.name if a >= b else scenario.second.name


def fused_at(rank_keyword: int, rank_vector: int) -> float:
    """The fused RRF score of an entry at these 1-based ranks in the two streams."""
    keyword = [f"k{n}" for n in range(1, rank_keyword)] + ["x"]
    vector = [f"v{n}" for n in range(1, rank_vector)] + ["x"]
    return rrf_fuse(
        [keyword, vector],
        k=CONFIG.rrf_k,
        weights=[CONFIG.weight_keyword, CONFIG.weight_vector],
    )["x"]


S1 = Scenario(
    "S1",
    "a 400-day entry with 3 helpful vs an unlinked 20-day correction with no feedback",
    Candidate("old_with_helpful", 400.0, (3, 0, 0)),
    Candidate("newer_correction", 20.0),
)
S2 = Scenario(
    "S2",
    "a two-year-old fact with 1 helpful (re-checked yesterday) vs one never checked",
    Candidate("rechecked_fact", 730.0, (1, 0, 0), confirmed_ago_days=1.0),
    Candidate("never_checked_fact", 730.0),
)
S3 = Scenario(
    "S3",
    "4 helpful and 1 fresh stale on a four-year-old entry vs an unrated one of the same age",
    Candidate("flagged_old_entry", 1460.0, (4, 1, 0)),
    Candidate("unrated_alternative", 1460.0),
)
S4 = Scenario(
    "S4",
    "importance 5 ranked 16th in both streams vs importance 3 ranked 1st in both",
    Candidate("important_loose_match", 5.0, fused=fused_at(16, 16), importance=5),
    Candidate("exact_match", 5.0, fused=fused_at(1, 1), importance=3),
)

SCENARIOS: tuple[Scenario, ...] = (S1, S2, S3, S4)

# Who wins today. A later ranking change that flips one edits this table in
# the same change (and its ADR says why); nothing else should move it.
#   S1  version grouping / supersession of the older version
#   S2  confirmation date as the recency clock (ADR 0071, setting on: still
#       the same winner, by a clear margin instead of a sliver)
#   S3  reserve "helpful evaporation" (helpfuls fade when a fresh stale lands)
#   S4  a narrowed importance factor
EXPECTED_WINNER: dict[str, str] = {
    "S1": "old_with_helpful",
    "S2": "rechecked_fact",
    "S3": "flagged_old_entry",
    "S4": "important_loose_match",
}
