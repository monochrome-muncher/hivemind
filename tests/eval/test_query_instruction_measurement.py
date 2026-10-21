"""The query instruction measurement (``tests/eval/query_instruction.py``,
ADR 0097) runs.

On the 4-dimension hash embedder its numbers mean nothing (the setting is
measured with a real model: ``make measure-query-instruction``), so this pins
the machinery only: the instructions it runs, the metric ranges, the report
and the JSON.
"""

from __future__ import annotations

import json

import pytest

from tests.eval.domains import DOMAINS
from tests.eval.query_instruction import (
    GENERIC_INSTRUCTION,
    extra_instructions,
    instructions,
    measure,
    percentile,
    report,
    to_json,
)
from tests.eval.threshold import domain_fixture
from tests.fakes import make_embedder


def test_instructions_are_none_generic_then_extras() -> None:
    assert instructions() == ["", GENERIC_INSTRUCTION]
    assert instructions(["mine", GENERIC_INSTRUCTION, "mine"]) == ["", GENERIC_INSTRUCTION, "mine"]


def test_extras_come_from_a_json_list() -> None:
    assert extra_instructions(None) == []
    assert extra_instructions("  ") == []
    assert extra_instructions('["a", "b"]') == ["a", "b"]
    with pytest.raises(ValueError, match="JSON list"):
        extra_instructions('{"a": 1}')


def test_percentile_interpolates() -> None:
    assert percentile([], 0.1) == 0.0
    assert percentile([5.0], 0.1) == 5.0
    assert percentile([0.0, 1.0, 2.0, 3.0, 4.0], 0.5) == 2.0
    assert percentile([0.0, 10.0], 0.1) == pytest.approx(1.0)


async def test_every_instruction_is_measured_and_the_report_reads() -> None:
    rows = await measure(make_embedder(), domain_fixture(DOMAINS[4]), ["variant"])
    assert [r.instruction for r in rows] == ["", GENERIC_INSTRUCTION, "variant"]
    for r in rows:
        assert 0.0 <= r.mrr <= 1.0 and 0.0 <= r.hit_at_1 <= r.hit_at_5 <= 1.0
        assert r.answer_similarity_p10 <= r.answer_similarity_mean + 1e-9
    text = report(rows, "hash")
    assert "baseline" in text and "generic" in text and "10th percentile" in text
    assert len(json.loads(to_json(rows, "hash"))["rows"]) == len(rows)
