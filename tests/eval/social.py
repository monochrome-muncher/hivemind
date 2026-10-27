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
    # A noted helpful report is newer than every flag ("last confirmed" set).
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    title: str
    first: Candidate
    second: Candidate


def score(candidate: Candidate, config: SearchConfig = CONFIG) -> float:
    """The final score of one candidate under ``config`` (the service's call)."""
    helpful, stale, wrong = candidate.counts
    kwargs = config.quality_kwargs(confirmed=candidate.confirmed)
    quality = feedback_quality(helpful, stale, wrong, **kwargs)
    return entry_score(
        candidate.fused,
        candidate.importance,
        FIXED_NOW - timedelta(days=candidate.age_days),
        quality,
        FIXED_NOW,
        config.half_life_days,
        recency_floor=config.recency_floor,
        social_max=config.social_max,
        narrow_importance=config.narrow_importance,
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
    Candidate("rechecked_fact", 730.0, (1, 0, 0)),
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

S5 = Scenario(
    "S5",
    "one wrong, then 3 noted helpful on an entry vs an unrated rival of the same age",
    Candidate("rebutted_flag", 60.0, (3, 0, 1), confirmed=True),
    Candidate("unrated_rival", 60.0),
)

SCENARIOS: tuple[Scenario, ...] = (S1, S2, S3, S4, S5)

# Who wins today. A later ranking change that flips one edits this table in
# the same change (and its ADR says why); nothing else should move it.
#   S1  supersession of the older version
#   S2  a stronger social signal for a re-checked old fact
#   S3  reserve "helpful evaporation" (helpfuls fade when a fresh stale lands)
#   S4  flipped by the narrowed importance factor (ADR 0088); the wide factor
#       it replaced had the loose match winning
#   S5  flipped by halving confirmed flags (ADR 0106); counted in full, it
#       takes five helpful reports to cancel one wrong
EXPECTED_WINNER: dict[str, str] = {
    "S1": "old_with_helpful",
    "S2": "rechecked_fact",
    "S3": "flagged_old_entry",
    "S4": "exact_match",
    "S5": "rebutted_flag",
}
