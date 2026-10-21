"""The version-grouping measurement (``tests/eval/versions.py``, ADR 0074) runs.

On the 4-dimension hash embedder its numbers mean nothing (the cut-off is
measured with a real model: ``make measure-versions``), so this pins the
machinery only: the fixtures are well formed, grouping off is the baseline
row, every tau of the sweep reports, counts pool by addition, and the
adoption rule says what the study says.
"""

from __future__ import annotations

import re

from tests.eval.domains import DOMAINS
from tests.eval.version_pairs import VERSION_DOMAINS
from tests.eval.versions import (
    SWEEP,
    SYSADMIN,
    Measurement,
    Row,
    adoption,
    measure,
    report,
    suggest,
)
from tests.fakes import FakeEmbedder, make_embedder


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def test_every_domain_has_enough_pairs_and_distractors() -> None:
    assert [v.domain for v in VERSION_DOMAINS] == list(DOMAINS)
    assert SYSADMIN in {v.name for v in VERSION_DOMAINS}
    for v in VERSION_DOMAINS:
        assert len(v.pairs) >= 6, v.name
        texts = [t for p in v.pairs for t in (p.old[1], p.new[1], p.distractor[1])]
        assert len(set(texts)) == len(texts), v.name
        pool = {e[1] for e in v.domain.entries}
        assert not pool & set(texts), v.name
        queries = [q for p in v.pairs for q in (p.query, p.old_query)]
        assert len(set(queries)) == len(queries), v.name


def test_the_older_statement_often_has_the_higher_lexical_overlap_with_the_query() -> None:
    for v in VERSION_DOMAINS:
        older = sum(
            len(_words(p.query) & _words(p.old[1])) > len(_words(p.query) & _words(p.new[1]))
            for p in v.pairs
        )
        assert older * 2 >= len(v.pairs), v.name
        for p in v.pairs:  # the distractor is another setting, not a rewording
            assert _words(p.distractor[1]) != _words(p.old[1])


async def test_the_sweep_reports_the_baseline_and_every_tau_for_every_domain() -> None:
    m = await measure(make_embedder())
    assert list(m.domains) == [d.name for d in DOMAINS]
    for rows in m.domains.values():
        assert [r.tau for r in rows] == [None, *SWEEP]
        base = rows[0]
        assert base.false_grouped == base.pairs_grouped == base.background_groups == 0
        assert base.pairs == 6 and base.distractors == 6
        assert 0.0 <= base.mrr_old <= 1.0 and 0.0 <= base.mrr_new <= 1.0
        assert all(r.queries == base.queries for r in rows)
    pooled = m.pooled()
    assert pooled[0].pairs == 30 and pooled[0].distractors == 30
    assert "Suggested HIVEMIND_VERSION_GROUP_MIN_SIMILARITY" in report(m)
    # A looser cut-off never groups fewer pairs than a stricter one.
    grouped = [r.pairs_grouped + r.false_grouped + r.background_groups for r in pooled[1:]]
    assert grouped == sorted(grouped, reverse=True)


def _row(tau: float | None, **kw: float) -> Row:
    base = {
        "distractors": 20,
        "pairs": 10,
        "queries": 40,
        "rr_new": 5.0,
        "rr_old": 5.0,
        "hits": 30.0,
    }
    return Row(tau, **{**base, **kw})  # type: ignore[arg-type]


def _measurement(candidate: Row, sysadmin: Row | None = None) -> Measurement:
    base = _row(None)
    return Measurement(
        "m",
        {
            "data": [base, candidate],
            SYSADMIN: [base, sysadmin or candidate],
        },
    )


def test_the_adoption_rule() -> None:
    good = _row(0.8, rr_new=6.0, false_grouped=0, pairs_grouped=9)
    [v] = adoption(_measurement(good))
    assert v.passes and suggest(_measurement(good)) == 0.8

    def failures(candidate: Row, sysadmin: Row | None = None) -> str:
        return "; ".join(adoption(_measurement(candidate, sysadmin))[0].failures)

    assert "not up" in failures(_row(0.8, rr_new=5.0))
    # Old-exact MRR may fall 0.02 (it can only fall), not more.
    assert "old-exact" not in failures(_row(0.8, rr_new=6.0, rr_old=4.9))  # -0.010
    assert "old-exact" in failures(_row(0.8, rr_new=6.0, rr_old=4.7))  # -0.030
    assert "hit@5" in failures(_row(0.8, rr_new=6.0, hits=29.0))
    assert "false grouping" in failures(_row(0.8, rr_new=6.0, false_grouped=3))  # 7.5% pooled
    # Pooled 5% passes only if every domain is within 10%: one domain at 15%.
    assert "false grouping" in failures(_row(0.8, rr_new=6.0, false_grouped=3))
    assert "sysadmin false grouping" in failures(
        _row(0.8, rr_new=6.0), _row(0.8, rr_new=6.0, false_grouped=2)
    )
    assert suggest(_measurement(_row(0.8, rr_new=5.0))) is None


class _RecordingEmbedder(FakeEmbedder):
    """The hash embedder, remembering every text it was asked to embed."""

    def __init__(self) -> None:
        super().__init__()
        self.texts: list[str] = []

    async def embed_text(self, text: str) -> list[float]:
        self.texts.append(text)
        return await super().embed_text(text)


async def test_a_query_instruction_reaches_the_embedder_without_changing_the_shape() -> None:
    domains = VERSION_DOMAINS[:1]
    plain = await measure(make_embedder(), domains)
    instruction = "Retrieve the entry that answers this question"
    recorder = _RecordingEmbedder()
    m = await measure(recorder, domains, query_instruction=instruction)
    assert any(t.startswith(f"Instruct: {instruction}\nQuery:") for t in recorder.texts)
    assert [r.tau for r in m.domains[domains[0].name]] == [
        r.tau for r in plain.domains[domains[0].name]
    ]
    assert f"Query instruction: `{instruction}`" in report(m)
    assert "Query instruction: none" in report(plain)
