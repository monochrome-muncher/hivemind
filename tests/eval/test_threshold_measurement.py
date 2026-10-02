"""The threshold measurement (``tests/eval/threshold.py``, ADR 0062) runs.

On the 4-dimension hash embedder its numbers mean nothing (the value is
measured with a real model: ``make measure-threshold``), so this pins the
machinery only: the sweep runs through the shipped pipeline, a threshold
of 0 is today's behaviour, and the suggestion keeps every answer.
"""

from __future__ import annotations

import pytest

from tests.eval.threshold import OFF_TOPIC_QUERIES, SWEEP, measure, report, suggest
from tests.fakes import make_embedder


async def test_the_sweep_reports_every_threshold() -> None:
    m = await measure(make_embedder())
    assert [r.threshold for r in m.rows] == list(SWEEP)
    assert m.off_topic == len(OFF_TOPIC_QUERIES)
    assert len(m.relevant_similarities) == m.answerable == 18
    kept = [r.answers_kept for r in m.rows]
    emptied = [r.vector_empty for r in m.rows]
    assert kept == sorted(kept, reverse=True)
    assert emptied == sorted(emptied)
    assert "Suggested HIVEMIND_VECTOR_MIN_SIMILARITY" in report(m)


@pytest.mark.parametrize(
    ("lowest", "expected"),
    [(0.62, 0.55), (0.60, 0.55), (0.599, 0.5), (0.04, 0.0), (-0.3, 0.0)],
)
def test_the_suggestion_stays_a_margin_below_the_weakest_answer(
    lowest: float, expected: float
) -> None:
    assert suggest([0.9, lowest, 0.7]) == pytest.approx(expected)
