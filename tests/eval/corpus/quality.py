"""Structural quality checks for the expanded corpus (see README.md).

No embedder: these are cheap lexical checks that catch a degenerate,
templated domain before it skews a measurement. The 2026-10-18 corpus had
ten domains whose summaries were one fill-in-the-blanks sentence ("The X is
the 5 with the 2 that are the critical and the 3 that are the standard"),
whose bodies repeated one boilerplate line, and whose queries were keyword
fragments. Each check below would have failed them; the limits sit well
clear of the hand-written domains.

``problems(domain, versions)`` returns a list of human-readable findings
(empty means clean). ``python -m tests.eval.corpus.quality [slug ...]``
prints them per domain.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Iterable
from itertools import combinations

from tests.eval.domains import Domain
from tests.eval.version_pairs import VersionDomain

# The largest share of summaries one 4-word phrase (numbers folded) may
# appear in. Hand-written domains peak at 14 of 128; the templated ones
# at 109-125.
MAX_SHARED_PHRASE = 16
# The most summaries that may open with the same two words.
MAX_SHARED_OPENING = 16
# Background entries whose word sets overlap this much are near-copies.
NEAR_COPY_JACCARD = 0.6
MAX_NEAR_COPIES = 3
# Queries of at least this many words count as full questions or
# descriptions; at least this many answerable queries must be one.
LONG_QUERY_WORDS = 5
MIN_LONG_QUERIES = 15
# Version-pair statements carry the same detail as background entries.
MIN_PAIR_AVG_WORDS = 8
SUMMARY_WORDS = (8, 45)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", re.sub(r"\d+(\.\d+)?", "0", text.lower()))


def _grams(text: str, n: int) -> set[str]:
    t = _tokens(text)
    return {" ".join(t[i : i + n]) for i in range(len(t) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def problems(domain: Domain, versions: VersionDomain) -> list[str]:
    """Every structural problem found in one domain (empty when clean)."""
    found: list[str] = []
    summaries = [e[1] for e in domain.entries]
    bodies = [e[3] for e in domain.entries if e[3]]

    for label, items in (("summary", summaries), ("body", bodies)):
        dupes = [t for t, n in Counter(items).items() if n > 1]
        if dupes:
            found.append(f"{len(dupes)} repeated {label} text(s), e.g. {dupes[0]!r}")

    for kind, summary, tags, _ in domain.entries:
        words = len(summary.split())
        if not SUMMARY_WORDS[0] <= words <= SUMMARY_WORDS[1]:
            found.append(f"summary of {words} words: {summary!r}")
        if kind not in ("fact", "insight", "decision"):
            found.append(f"unknown kind {kind!r}: {summary!r}")
        if not 1 <= len(tags) <= 3:
            found.append(f"{len(tags)} tags: {summary!r}")
        if len(tags) > 3 and all(len(tag) == 1 for tag in tags):
            found.append(f"tags are one string split into letters: {summary!r}")

    phrase, count = _most_common(g for s in summaries for g in _grams(s, 4))
    if count > MAX_SHARED_PHRASE:
        found.append(f"templated summaries: {count} share the phrase {phrase!r}")
    opening, count = _most_common(" ".join(_tokens(s)[:2]) for s in summaries)
    if count > MAX_SHARED_OPENING:
        found.append(f"templated summaries: {count} open with {opening!r}")

    sets = [set(_tokens(s)) for s in summaries]
    copies = [
        (summaries[i], summaries[j])
        for i, j in combinations(range(len(sets)), 2)
        if _jaccard(sets[i], sets[j]) >= NEAR_COPY_JACCARD
    ]
    if len(copies) > MAX_NEAR_COPIES:
        found.append(f"{len(copies)} near-copy summary pairs, e.g. {copies[0]!r}")

    queries = [q for q, _ in domain.answerable] + list(domain.unanswered)
    lowered = {s.lower() for s in summaries}
    for q in queries:
        if q.lower() in lowered:
            found.append(f"query copies a summary verbatim: {q!r}")
    dupes = [q for q, n in Counter(queries).items() if n > 1]
    if dupes:
        found.append(f"{len(dupes)} repeated query text(s), e.g. {dupes[0]!r}")
    for q, idx in domain.answerable:
        if not idx or any(not 0 <= i < len(summaries) for i in idx):
            found.append(f"answerable query with bad indices {idx}: {q!r}")
    long_queries = sum(len(q.split()) >= LONG_QUERY_WORDS for q, _ in domain.answerable)
    if long_queries < MIN_LONG_QUERIES:
        found.append(
            f"only {long_queries} of {len(domain.answerable)} answerable queries "
            f"have {LONG_QUERY_WORDS}+ words (keyword fragments only)"
        )

    pair_words = [len(p.new[1].split()) for p in versions.pairs]
    if pair_words and sum(pair_words) / len(pair_words) < MIN_PAIR_AVG_WORDS:
        found.append(
            f"version-pair statements average {sum(pair_words) / len(pair_words):.1f} words"
        )
    for p in versions.pairs:
        if p.old[1] == p.new[1]:
            found.append(f"version pair with identical statements: {p.old[1]!r}")
        if p.distractor[1] in (p.old[1], p.new[1]):
            found.append(f"distractor copies the pair: {p.distractor[1]!r}")
        for statement in (p.old[1], p.new[1], p.distractor[1]):
            if statement in lowered or statement in summaries:
                found.append(f"version-pair text copies a background entry: {statement!r}")
    return found


def _most_common(items: Iterable[str]) -> tuple[str, int]:
    counts = Counter(items)
    return counts.most_common(1)[0] if counts else ("", 0)


def main(argv: list[str]) -> int:
    from tests.eval.corpus import EXTENDED_DOMAINS, EXTENDED_VERSION_DOMAINS

    wanted = {a.replace("_", " ") for a in argv}
    bad = 0
    for domain, versions in zip(EXTENDED_DOMAINS, EXTENDED_VERSION_DOMAINS, strict=True):
        if wanted and domain.name not in wanted:
            continue
        found = problems(domain, versions)
        bad += bool(found)
        print(f"{domain.name}: {'clean' if not found else f'{len(found)} problem(s)'}")
        for line in found[:20]:
            print(f"  - {line}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
