"""A recency profile per kind (ADR 0072) over the age-varied fixture.

The slices are embedding-free in spirit (the 4-dimension hash embedder, keyword
overlap does the work), so they pin direction and arithmetic, not a gate: the
pass rule (no domain or pooled hit@5 / MRR drop beyond 0.01, the timeless
slice improves) is decided by ``make measure-ranking`` with the real
embedders. The numbers are in docs/retrieval-experiments.md.

On this fixture the profile is NOT a win: the timeless slice does not improve
(it stays at 0.617 at the fixture's ages and falls 0.667 -> 0.633 once the
newer currency members are aged), and the aged-currency pairs improve only
where the profile's longer half-life keeps the newer member's recency above
the floor. Recorded as measured; nothing is tuned to hide it.
"""

from __future__ import annotations

import pytest

from hivemind.config import SearchConfig
from tests.eval.confirmation import PROFILE_MEDIUM, PROFILE_SLOW
from tests.eval.temporal_runner import DECAY_ON, aggregate_slices, measure_slices
from tests.eval.test_aged_currency import AGES, NEWER_MEMBERS

SHIPPED_FLOOR = SearchConfig().recency_floor


async def report(
    profile: tuple[tuple[object, float], ...] | None, age: float | None = None
) -> dict[str, dict[str, float]]:
    slices = await measure_slices(
        lambda _q: DECAY_ON,
        recency_floor=SHIPPED_FLOOR,
        age_overrides=None if age is None else dict.fromkeys(NEWER_MEMBERS, age),
        recency_by_kind=profile,  # type: ignore[arg-type]
    )
    return aggregate_slices(slices)


async def test_no_profile_is_the_shipped_pipeline() -> None:
    """An empty profile measures exactly as no profile (bit-identical scores)."""
    for age in (None, *AGES):
        assert await report(None, age) == await report((), age)


@pytest.mark.parametrize("profile", [PROFILE_SLOW, PROFILE_MEDIUM], ids=["180/365", "90/180"])
async def test_the_profile_on_the_fixture_as_written(
    profile: tuple[tuple[object, float], ...],
) -> None:
    """Machinery-only numbers (hash embedder): the currency pairs gain, the
    timeless slice and old-exact do not move."""
    shipped = await report(None)
    profiled = await report(profile)
    assert shipped["currency_pair"]["mrr"] == pytest.approx(7 / 9)
    assert profiled["currency_pair"]["mrr"] == pytest.approx(5 / 6)
    assert profiled["timeless"] == shipped["timeless"]
    assert profiled["old_exact"] == shipped["old_exact"]


async def test_the_profile_on_aged_currency_pairs() -> None:
    """Newer members aged 30 days (past the shipped floor age, 9.7): the slow
    profile keeps a little recency on the decisions and insights among them
    (MRR 0.583 -> 0.611), and loses on the timeless slice (0.667 -> 0.633),
    where a longer half-life keeps old entries nearer the top. At 120 days
    nothing is left to separate them, and nothing moves."""
    shipped = await report(None, 30.0)
    slow = await report(PROFILE_SLOW, 30.0)
    assert shipped["currency_pair"]["mrr"] == pytest.approx(7 / 12)
    assert slow["currency_pair"]["mrr"] == pytest.approx(11 / 18)
    assert shipped["timeless"]["mrr"] == pytest.approx(2 / 3)
    assert slow["timeless"]["mrr"] == pytest.approx(19 / 30)
    assert await report(PROFILE_SLOW, 120.0) == await report(None, 120.0)
