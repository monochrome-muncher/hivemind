"""The importance measurement (``tests/eval/importance.py``, ADR 0088) runs.

On the 4-dimension hash embedder its numbers mean nothing (the setting is
measured with a real model: ``make measure-importance``), so this pins the
machinery only: the assignments are deterministic and shaped as described,
the equal assignment ranks identically with the factor off and on, and the
report reads.
"""

from __future__ import annotations

import json

from hivemind.memstore import MemoryStore
from tests.eval.domains import DOMAINS
from tests.eval.importance import (
    ASSIGNMENTS,
    importances,
    measure,
    report,
    seed_pool,
    to_json,
)
from tests.eval.threshold import combined_fixture, domain_fixture
from tests.fakes import FakeEmbedder, make_clock, make_embedder


def test_equal_is_all_threes() -> None:
    assert set(importances(domain_fixture(DOMAINS[0]), "equal")) == {3}


def test_random_is_seeded_and_spread() -> None:
    fixture = domain_fixture(DOMAINS[0])
    values = importances(fixture, "random")
    assert values == importances(fixture, "random")
    assert set(values) == {1, 2, 3, 4, 5}
    assert values != importances(fixture, "random", seed=1)


def test_aligned_gives_every_answer_four_or_five() -> None:
    fixture = domain_fixture(DOMAINS[1])
    values = importances(fixture, "aligned")
    answers = {i for _q, rel in fixture.answerable for i in rel}
    assert {values[i] for i in answers} <= {4, 5}
    others = [v for i, v in enumerate(values) if i not in answers]
    assert set(others) >= {1, 2}


def test_an_entry_has_the_same_importance_alone_and_pooled() -> None:
    alone = domain_fixture(DOMAINS[2])
    pooled = combined_fixture()
    offset = sum(len(d.entries) for d in DOMAINS[:2])
    alone_values = importances(alone, "random")
    assert importances(pooled, "random")[offset : offset + len(alone_values)] == alone_values


async def test_seeding_writes_the_importance() -> None:
    fixture = domain_fixture(DOMAINS[3])
    store = MemoryStore(make_clock())
    ids = await seed_pool(make_embedder(), store, fixture, "aligned")
    entries = await store.get_entries(ids)
    assert [entries[i].importance for i in ids] == importances(fixture, "aligned")


async def test_equal_ranks_the_same_off_and_on_and_the_report_reads() -> None:
    rows = await measure(make_embedder(), domain_fixture(DOMAINS[4]))
    assert len(rows) == len(ASSIGNMENTS) * 2
    off, on = [r for r in rows if r.assignment == "equal"]
    assert (off.hit_at_1, off.hit_at_5, off.mrr) == (on.hit_at_1, on.hit_at_5, on.mrr)
    assert all(0.0 <= r.mrr <= 1.0 and 0.0 <= r.hit_at_1 <= r.hit_at_5 <= 1.0 for r in rows)
    text = report(rows, "hash")
    assert "aligned" in text and "narrow on" in text
    assert len(json.loads(to_json(rows, "hash"))["rows"]) == len(rows)


class _RecordingEmbedder(FakeEmbedder):
    """The hash embedder, remembering every text it was asked to embed."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    async def embed_text(self, text: str) -> list[float]:
        self.texts.append(text)
        return await super().embed_text(text)


async def test_a_query_instruction_reaches_the_embedder_without_changing_the_shape() -> None:
    fixture = domain_fixture(DOMAINS[4])
    plain = await measure(make_embedder(), fixture)
    instruction = "Retrieve the entry that answers this question"
    recorder = _RecordingEmbedder()
    rows = await measure(recorder, fixture, query_instruction=instruction)
    assert any(t.startswith(f"Instruct: {instruction}\nQuery:") for t in recorder.texts)
    assert [(r.assignment, r.narrow, r.queries) for r in rows] == [
        (r.assignment, r.narrow, r.queries) for r in plain
    ]
    assert f"Query instruction: `{instruction}`" in report(rows, "hash", instruction)
    assert "Query instruction: none" in report(plain, "hash")
    assert json.loads(to_json(rows, "hash", instruction))["query_instruction"] == instruction
