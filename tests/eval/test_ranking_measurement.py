"""The ranking measurement (``tests/eval/ranking.py``) runs.

On the 4-dimension hash embedder its numbers mean nothing (the variants are
measured with a real model: ``make measure-ranking``), so this pins
the machinery only: the seeding is deterministic, every variant runs over
the same pool, and the report reads.
"""

from __future__ import annotations

from dataclasses import replace

from hivemind.config import SearchConfig
from hivemind.memstore import MemoryStore
from tests.eval.domains import DOMAINS
from tests.eval.ranking import (
    CONFIRM_MAX_DAYS,
    CONFIRM_MIN_DAYS,
    MAX_AGE_DAYS,
    VARIANTS,
    Row,
    Variant,
    confirmation_ago_days,
    drops,
    entry_age_days,
    fixtures,
    measure,
    pick_fraction,
    report,
    seed,
)
from tests.eval.threshold import domain_fixture
from tests.fakes import make_clock, make_embedder


def test_ages_stay_in_range_and_spread() -> None:
    ages = [entry_age_days(i) for i in range(160)]
    assert 0.0 <= min(ages) < 5.0 and 395.0 < max(ages) <= MAX_AGE_DAYS
    assert sum(a > 9.7 for a in ages) > 150  # past the floor's age, so the floor matters


def test_the_pick_is_a_deterministic_fifth() -> None:
    summaries = [e[1] for e in DOMAINS[0].entries]
    picked = pick_fraction(summaries)
    assert picked == pick_fraction(list(summaries))
    assert len(picked) == round(len(summaries) * 0.2) == 6
    reordered = list(reversed(summaries))
    assert {summaries[i] for i in picked} == {reordered[i] for i in pick_fraction(reordered)}


async def test_seeding_writes_the_pool_and_a_fifth_of_helpful_reports() -> None:
    fixture = domain_fixture(DOMAINS[1])
    store = MemoryStore(make_clock())
    ids = await seed(make_embedder(), store, fixture)
    assert len(ids) == len(fixture.entries)
    signals = await store.feedback_signals(ids)
    confirmed = [i for i in ids if signals[i].last_confirmed_at is not None]
    assert len(confirmed) == round(len(ids) * 0.2)
    assert all(signals[i].counts == (1, 0, 0) for i in confirmed)
    ago = [confirmation_ago_days(n) for n in range(32)]
    assert all(CONFIRM_MIN_DAYS <= a <= CONFIRM_MAX_DAYS for a in ago)


async def test_every_variant_runs_and_the_report_reads() -> None:
    names = [f.name for f in fixtures()]
    assert len(names) == 6 and names[:2] == ["all domains together", "data analysis"]
    # One fixture is enough to run: the variants take the same path on each.
    rows = await measure(make_embedder(), domain_fixture(DOMAINS[1]))
    assert len(rows) == len(VARIANTS)
    assert all(0.0 <= r.hit_at_k <= 1.0 and 0.0 <= r.mrr <= 1.0 for r in rows)
    text = report(rows, "hash")
    assert "recency hint: ignore_age" in text and "Pass rule" in text


async def test_a_variant_is_a_set_of_config_overrides() -> None:
    """A variant that changes nothing measures the same as the baseline."""
    same = (Variant("a", {}), Variant("b", {"social_max": SearchConfig().social_max}))
    rows = await measure(make_embedder(), domain_fixture(DOMAINS[2]), same)
    assert (rows[0].hit_at_k, rows[0].mrr) == (rows[1].hit_at_k, rows[1].mrr)
    assert drops(rows) == []


def test_drops_flag_a_fall_beyond_the_pass_rule() -> None:
    base = Row("f", "base", 10, 0.80, 0.60)
    assert drops([base, replace(base, variant="small", mrr=0.595)]) == []
    assert drops([base, replace(base, variant="big", mrr=0.58)]) != []
    assert drops([base, replace(base, variant="hit", hit_at_k=0.7)]) != []
