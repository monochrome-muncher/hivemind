"""The LongMemEval measurement (``tests/eval/longmemeval.py``) runs.

On the 4-dimension hash embedder its numbers mean nothing (the real run is
``make measure-longmemeval``), so this pins the machinery only: abstention
excluded, deterministic stratified sampling, the metrics on a constructed
ranking, the summary and body bounds, the report and the JSON, and the disk
embedding cache.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hivemind.domain.validation import MAX_BODY_CHARS
from tests.eval.embed_cache import DiskCachingEmbedder
from tests.eval.longmemeval import (
    SUMMARY_MAX_CHARS,
    VARIANTS,
    measure,
    parse_date,
    per_type_limit,
    report,
    score,
    seed,
    select,
    session_body,
    session_summary,
    to_json,
)
from tests.fakes import make_embedder


def _session(*turns: str) -> list[dict[str, str]]:
    roles = ("user", "assistant")
    return [{"role": roles[i % 2], "content": t} for i, t in enumerate(turns)]


def _question(qid: str, qtype: str, answers: list[str]) -> dict[str, object]:
    sessions = {
        "s1": _session("I adopted a greyhound named Biscuit", "Lovely name"),
        "s2": _session("My sourdough starter keeps dying", "Feed it daily"),
        "s3": _session("Planning a trip to Lisbon in May", "Book early"),
    }
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": "What is the name of my greyhound?",
        "question_date": "2023/05/30 (Tue) 23:40",
        "answer": "Biscuit",
        "answer_session_ids": answers,
        "haystack_session_ids": list(sessions),
        "haystack_dates": [
            "2023/05/20 (Sat) 02:21",
            "2023/05/22 (Mon) 10:00",
            "2023/05/25 (Thu) 18:30",
        ],
        "haystack_sessions": list(sessions.values()),
    }


QUESTIONS = [
    _question("q1", "single-session-user", ["s1"]),
    _question("q2", "multi-session", ["s1", "s3"]),
    _question("q3_abs", "single-session-user", []),
]


def test_dates_parse_as_utc() -> None:
    assert parse_date("2023/05/30 (Tue) 23:40") == datetime(2023, 5, 30, 23, 40, tzinfo=UTC)
    with pytest.raises(ValueError, match="date"):
        parse_date("30 May 2023")


def test_abstention_is_excluded_and_sampling_is_stratified_and_stable() -> None:
    many = [_question(f"a{i}", "multi-session", ["s1"]) for i in range(6)]
    many += [_question(f"b{i}", "knowledge-update", ["s1"]) for i in range(6)]
    many += [_question("c_abs", "knowledge-update", [])]
    everything = select(many)
    assert len(everything) == 12 and all(
        not str(q["question_id"]).endswith("_abs") for q in everything
    )
    picked = select(many, 2)
    assert [q["question_type"] for q in picked].count("multi-session") == 2
    assert [q["question_type"] for q in picked].count("knowledge-update") == 2
    assert [q["question_id"] for q in picked] == [
        q["question_id"] for q in select(list(reversed(many)), 2)
    ]
    assert per_type_limit(None) is None and per_type_limit("3") == 3
    with pytest.raises(ValueError):
        per_type_limit("0")


def test_score_on_a_constructed_ranking() -> None:
    ranked = ["x", "a", "y", "z", "w", "b"]  # answers a at 2, b at 6
    got = score(ranked, ["a", "b"])
    assert got["recall_any_5"] == 1.0 and got["recall_all_5"] == 0.0
    assert got["recall_any_10"] == 1.0 and got["recall_all_10"] == 1.0
    ideal = 1 / math.log2(2) + 1 / math.log2(3)
    assert got["ndcg_10"] == pytest.approx((1 / math.log2(3) + 1 / math.log2(7)) / ideal)
    miss = score(["x", "y"], ["a"])
    assert set(miss.values()) == {0.0}


def test_summary_and_body_respect_the_bounds() -> None:
    long = _session("  word   " * 200, "reply")
    summary = session_summary(long)
    assert len(summary) <= SUMMARY_MAX_CHARS and "  " not in summary
    assert session_summary([]) == "(empty session)"
    assert session_summary(_session("   ")) == "(empty session)"
    huge = _session("x" * (MAX_BODY_CHARS + 50), "y")
    body = session_body(huge)
    assert body is not None and len(body) <= MAX_BODY_CHARS
    assert session_body(_session("hi", "yo")) == "user: hi\nassistant: yo"


async def test_seed_writes_one_entry_per_session() -> None:
    _, by_entry = await seed(QUESTIONS[0], make_embedder())
    assert sorted(by_entry.values()) == ["s1", "s2", "s3"]


async def test_measure_report_and_json() -> None:
    lines: list[str] = []
    rows = await measure(make_embedder(), QUESTIONS, progress=lines.append)
    assert len(rows) == 2 * len(VARIANTS) and len(lines) == 2  # q3_abs dropped
    assert {r.variant for r in rows} == {v.name for v in VARIANTS}
    for r in rows:
        assert 0.0 <= r.ndcg_10 <= 1.0
        assert r.recall_any_5 <= r.recall_any_10 and r.recall_all_5 <= r.recall_all_10
        assert r.recall_all_10 <= r.recall_any_10
    text = report(rows, "hash", "all")
    assert "**shipped**" in text and "multi-session" in text and "overall" in text
    assert "`hash`" in text and "Questions: 2" in text
    data = json.loads(to_json(rows, "hash", "all"))
    assert len(data["rows"]) == len(rows) and data["model"] == "hash"


async def test_disk_cache_reuses_vectors_across_instances(tmp_path: Path) -> None:
    calls: list[str] = []

    class Counting:
        def __init__(self) -> None:
            self._inner = make_embedder()
            self.dimension = self._inner.dimension
            self.model_name = self._inner.model_name

        def entry_embeddable_text(self, draft):  # type: ignore[no-untyped-def]
            return self._inner.entry_embeddable_text(draft)

        async def embed_text(self, text: str) -> list[float]:
            calls.append(text)
            return await self._inner.embed_text(text)

        async def embed_entry(self, draft):  # type: ignore[no-untyped-def]
            calls.append("entry")
            return await self._inner.embed_entry(draft)

    first = DiskCachingEmbedder(Counting(), tmp_path)
    vector = await first.embed_text("hello world")
    assert await first.embed_text("hello world") == vector and calls == ["hello world"]
    first.close()
    second = DiskCachingEmbedder(Counting(), tmp_path)  # a re-run
    assert await second.embed_text("hello world") == vector
    assert calls == ["hello world"]
    second.close()
