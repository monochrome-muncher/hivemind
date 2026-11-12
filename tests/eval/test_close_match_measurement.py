"""The close-match measurement (``tests/eval/close_match.py``, ADR 0148) runs.

On the hash embedder its numbers mean nothing (the bar is measured with a real
model: ``make measure-close-match``); this pins the machinery only.
"""

from __future__ import annotations

from tests.eval.close_match import BARS, measure, report
from tests.eval.domains import DOMAINS
from tests.eval.threshold import domain_fixture
from tests.fakes import make_embedder


async def test_one_domain_pool_reports_every_bar() -> None:
    fixture = domain_fixture(DOMAINS[0])
    result = await measure(make_embedder(), fixture)
    assert len(result.answerable) == len(fixture.answerable)
    assert len(result.unanswered_hard) == len(DOMAINS[0].unanswered)
    assert len(result.unanswered_hard) + len(result.unanswered_other) == len(fixture.off_topic)
    text = report(fixture.name, [result])
    assert all(f"| {bar:.2f} |" in text for bar in BARS)
