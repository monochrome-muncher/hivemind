"""Aged currency pairs: does recency still help once the newer member is old? (ADR 0070)

The fixture's currency pairs (``tests/eval/temporal.py`` entries 4/5, 6/7,
8/9) have an old member aged 300 to 400 days and a newer member aged 5 to
12 days, so the newer one gets a real recency edge. This slice moves the
NEWER member to 30, 60 and 120 days and leaves the old member where it is.
Both members are then past the recency floor (0.5 ** (age / 30) < 0.8 from
about 9.7 days), so both carry the same recency factor, 0.8, and recency
can no longer separate them. What is left is the lexical match, which
favours the old member by construction, plus the social factor, which is
neutral here (no feedback).

It measures hit@5 and MRR for the newer member, with the shipped config
and with decay off, and records them as a baseline. It pins direction and
arithmetic, not a gate. The numbers are in docs/retrieval-experiments.md;
the confirmation-date clock that was measured against this baseline was
removed (ADR 0103).
"""

from __future__ import annotations

from hivemind.config import SearchConfig
from tests.eval.temporal_runner import (
    DECAY_OFF,
    DECAY_ON,
    aggregate_slices,
    measure_slices,
)

NEWER_MEMBERS = (5, 7, 9)
AGES = (30.0, 60.0, 120.0)
SHIPPED_FLOOR = SearchConfig().recency_floor


async def measure_slice(
    name: str,
    age: float | None,
    *,
    shipped: bool,
) -> dict[str, float]:
    """One slice's report; ``age=None`` is the fixture as written."""
    overrides = None if age is None else dict.fromkeys(NEWER_MEMBERS, age)
    slices = await measure_slices(
        lambda _q: DECAY_ON if shipped else DECAY_OFF,
        recency_floor=SHIPPED_FLOOR if shipped else None,
        age_overrides=overrides,
    )
    return aggregate_slices(slices)[name]


async def measure(age: float | None, *, shipped: bool) -> dict[str, float]:
    """The currency-pair slice's report; ``age=None`` is the fixture as written."""
    return await measure_slice("currency_pair", age, shipped=shipped)


async def test_aged_newer_members_lose_the_recency_edge_the_fixture_gives_them() -> None:
    """Baseline: with the newer member at 5-12 days the shipped config beats decay
    off on MRR; at 30, 60 and 120 days the shipped MRR is the same number for
    all three (the floor makes recency flat) and no better than decay off,
    while hit@5 stays 1.0 throughout."""
    as_written = await measure(None, shipped=True)
    as_written_off = await measure(None, shipped=False)
    assert as_written["mrr"] > as_written_off["mrr"]

    shipped = {age: await measure(age, shipped=True) for age in AGES}
    off = {age: await measure(age, shipped=False) for age in AGES}
    for age in AGES:
        assert shipped[age]["hit_at_k"] == off[age]["hit_at_k"] == 1.0
        assert shipped[age]["mrr"] <= off[age]["mrr"]
    # Past the floor the recency factor is the same constant for any age.
    assert shipped[30.0]["mrr"] == shipped[60.0]["mrr"] == shipped[120.0]["mrr"]
    assert off[30.0]["mrr"] == off[60.0]["mrr"] == off[120.0]["mrr"]


def test_the_aged_ages_are_past_the_recency_floor() -> None:
    """The construction's premise: every aged member is below the floor's age."""
    config = SearchConfig()
    assert config.recency_floor is not None
    assert all(0.5 ** (age / config.half_life_days) < config.recency_floor for age in AGES)
