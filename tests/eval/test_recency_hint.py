"""The recency hint (ADR 0073) over the age-varied fixture.

Each query carries a ``temporal`` label, which is the oracle for the hint: an
agent that knows its question asks for the current state sends
``prefer_recent``, one that asks a timeless question sends ``ignore_age``.
The 4-dimension hash embedder makes these numbers machinery-only; they pin
direction and arithmetic, not a gate. The real-embedder table is in
docs/retrieval-experiments.md (``make measure-ranking``).

Recorded as measured, nothing tuned to hide it: ``ignore_age`` is a clear win
on the timeless slice (0.617 -> 0.900, the study's figure), and costs the
currency pairs; ``prefer_recent`` is not a win on this fixture under either
floor (0.5, shipped: no slice improves and the old-exact answer to a
temporal-worded query is lost; no floor: one currency pair gains a rank and
non-temporal queries collapse).
"""

from __future__ import annotations

import pytest

from hivemind.config import PREFER_RECENT_FLOOR, SearchConfig
from hivemind.domain.recency import RecencyHint
from tests.eval.temporal import LabelledQuery
from tests.eval.temporal_runner import (
    DECAY_ON,
    SliceReports,
    aggregate_slices,
    measure_slices,
)

FLOOR = SearchConfig().recency_floor
PREFER, IGNORE, DEFAULT = RecencyHint.PREFER_RECENT, RecencyHint.IGNORE_AGE, RecencyHint.DEFAULT


async def mrr(rule=None, *, floor: float | None = PREFER_RECENT_FLOOR) -> dict[str, float]:
    reports: SliceReports = aggregate_slices(
        await measure_slices(
            lambda _q: DECAY_ON, recency_floor=FLOOR, hint=rule, prefer_recent_floor=floor
        )
    )
    return {name: r["mrr"] for name, r in reports.items()}


def oracle(q: LabelledQuery) -> RecencyHint:
    return PREFER if q.temporal else IGNORE


async def test_the_default_hint_is_the_shipped_pipeline() -> None:
    assert await mrr(lambda _q: DEFAULT) == await mrr(None)


async def test_ignore_age_on_every_query_matches_a_floor_of_one() -> None:
    """ignore_age is the study's floor-1.0 scratch run, without its confirmation
    side effects: timeless MRR 0.617 -> 0.900, currency pairs 0.778 -> 0.611."""
    shipped = await mrr(None)
    ignore = await mrr(lambda _q: IGNORE)
    unity = aggregate_slices(await measure_slices(lambda _q: DECAY_ON, recency_floor=1.0))
    assert shipped["timeless"] == pytest.approx(0.617, abs=5e-4)
    assert ignore["timeless"] == pytest.approx(0.900)
    assert ignore["currency_pair"] == pytest.approx(0.611, abs=5e-4)
    assert ignore == pytest.approx({n: r["mrr"] for n, r in unity.items()})


async def test_the_oracle_hint_per_slice() -> None:
    shipped = await mrr(None)
    hinted = await mrr(oracle)
    assert hinted["non_temporal"] == pytest.approx(0.917, abs=5e-4)
    assert hinted["timeless"] == pytest.approx(0.900)
    assert hinted["currency_pair"] == pytest.approx(shipped["currency_pair"])
    # The temporal-worded query whose right answer is the old exact match
    # loses to a lower floor: the price of prefer_recent on this fixture.
    assert hinted["old_exact"] == pytest.approx(0.750)
    assert hinted["temporal"] < shipped["temporal"]


async def test_prefer_recent_is_not_a_win_on_this_fixture() -> None:
    shipped = await mrr(None)
    floor_half = await mrr(lambda _q: PREFER)
    assert PREFER_RECENT_FLOOR == 0.5
    assert floor_half["currency_pair"] == pytest.approx(shipped["currency_pair"])
    assert floor_half["old_exact"] < shipped["old_exact"]
    assert floor_half["all"] < shipped["all"]
    no_floor = await mrr(lambda _q: PREFER, floor=None)
    assert no_floor["currency_pair"] == pytest.approx(0.833, abs=5e-4)
    assert no_floor["non_temporal"] < 0.25  # an unbounded term buries exact matches
    assert no_floor["all"] < floor_half["all"]


async def test_the_best_use_is_ignore_age_for_timeless_questions_only() -> None:
    """Where the agent is sure its question is timeless and says nothing else."""
    best = await mrr(lambda q: DEFAULT if q.temporal else IGNORE)
    shipped = await mrr(None)
    assert best["all"] == pytest.approx(0.883, abs=5e-4)
    assert best["temporal"] == pytest.approx(shipped["temporal"])
