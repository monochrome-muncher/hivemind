"""The expanded corpus stays realistic: no templated domain (see corpus/quality.py).

Lexical checks only, no embedder, so this runs in the normal suite.
"""

from __future__ import annotations

import pytest

from tests.eval.corpus import EXTENDED_DOMAINS, EXTENDED_VERSION_DOMAINS
from tests.eval.corpus.quality import problems
from tests.eval.corpus.specific_queries import SPECIFIC_ANSWERABLE, SPECIFIC_PAIRS
from tests.eval.domains import Domain
from tests.eval.version_pairs import VersionDomain

PAIRS = list(zip(EXTENDED_DOMAINS, EXTENDED_VERSION_DOMAINS, strict=True))


@pytest.mark.parametrize(("domain", "versions"), PAIRS, ids=[d.name for d, _ in PAIRS])
def test_domain_is_clean(domain: Domain, versions: VersionDomain) -> None:
    assert problems(domain, versions) == []


@pytest.mark.parametrize(("domain", "versions"), PAIRS, ids=[d.name for d, _ in PAIRS])
def test_domain_sizes(domain: Domain, versions: VersionDomain) -> None:
    assert len(domain.entries) == 128
    assert len(versions.pairs) == 16
    assert len(domain.answerable) == 40
    assert len(domain.unanswered) == 12


def test_the_template_is_caught() -> None:
    entries = tuple(
        (
            "fact",
            f"The access review score is the {n} with the 5 that are the critical",
            ("a",),
            None,
        )
        for n in range(40)
    )
    templated = Domain("templated", entries, (("access review", (0,)),), ())
    found = problems(templated, VersionDomain(templated, ()))
    assert any("templated summaries" in p for p in found)
    assert any("near-copy" in p for p in found)
    assert any("keyword fragments" in p for p in found)


@pytest.mark.parametrize(("domain", "versions"), PAIRS, ids=[d.name for d, _ in PAIRS])
def test_specific_queries_stay_aligned(domain: Domain, versions: VersionDomain) -> None:
    """The de-vagued variants are index-aligned with the queries they replace."""
    assert len(SPECIFIC_ANSWERABLE[domain.name]) == len(domain.answerable)
    assert len(SPECIFIC_PAIRS[domain.name]) == len(versions.pairs)
