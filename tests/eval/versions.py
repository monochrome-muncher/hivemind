"""Measure the cut-off for grouping unlinked versions (ADR 0074).

Whether two entries are versions of one fact depends on the embedding
model, so the cut-off ``HIVEMIND_VERSION_GROUP_MIN_SIMILARITY`` (tau) is
measured with the model a deployment runs: ``make measure-versions``. For
each domain the pool is the domain's own entries plus pairs of an older
and a newer unlinked statement of one fact and same-system distractors
(five domains in ``version_pairs.py``; the expanded sixteen-domain corpus
in ``tests/eval/corpus``). For each tau of the sweep, and for grouping
off, it runs the shipped search and reports, per domain and pooled:

* false grouping: the share of distractors grouped with a version member or
  with another distractor, in a search for their pair's fact;
* true grouping: the share of version pairs grouped in a search for the fact;
* newer-member MRR: reciprocal rank of the newer entry for the query about
  each fact (asked in the older statement's words);
* old-exact MRR: the same for the older entry and a query quoting it;
* hit@5 over every query of the pool (those two, plus the domain's own
  answerable queries), pooled across domains.

Set ``EVAL_QUERY_INSTRUCTION`` to a literal instruction string to run the
whole measurement with ``SearchConfig.query_instruction`` on (ADR 0097): the
search embeds the wrapped query and entries are embedded as before. Unset or
empty runs with no instruction; each report states
the instruction in use.

The adoption rule is the study's (2026-10-10 proposal, section 6.1). Run
it with the deployment's embedder (``HIVEMIND_EMBEDDING_*``);
``tests/eval/test_version_measurement.py`` runs it on the hash embedder,
which checks the machinery only: its numbers mean nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import timedelta

from hivemind.domain.entry import EntryDraft, Kind
from hivemind.memstore import MemoryStore
from hivemind.ports import Embedder
from hivemind.retrieval.eval import hit_at_k, reciprocal_rank
from hivemind.services.search import Hit, SearchService
from hivemind.services.write import WriteService
from tests.eval.version_pairs import SYSADMIN, VERSION_DOMAINS, Entry, VersionDomain
from tests.fakes import FIXED_NOW, make_clock, make_search_config

K = 5
SWEEP = tuple(round(0.70 + 0.05 * i, 2) for i in range(6))  # 0.70 .. 0.95
# The study's adoption rule.
POOLED_FALSE_MAX = 0.05
DOMAIN_FALSE_MAX = 0.10
EPS = 1e-9
# Grouping always moves the newer member ahead, so old-exact MRR can only
# fall; it may fall this far (the confirmation-clock rule's tolerance).
OLD_EXACT_TOLERANCE = 0.02
FLEET_A = "fleet-a"
FLEET_B = "fleet-b"


@dataclass(frozen=True, slots=True)
class Constellation:
    """How a pool's entries are scoped, one role at a time.

    ADR 0074's fleet rule groups only within one fleet, or between two
    org-scoped entries, so the constellation changes what *can* group,
    not the similarity values. ``assign`` returns (scope, fleet_id) for a
    role: ``background`` (index into the pool), ``old`` / ``new`` / ``distractor``
    (index into the pairs).
    """

    name: str
    note: str

    def assign(self, role: str, index: int) -> tuple[str, str | None]:
        raise NotImplementedError


ORG_ONLY = Constellation(
    "org only (legacy)",
    "every entry org-scoped; the 2026-10-11 measurement's shape",
)

ISOLATED_FLEET = Constellation(
    "isolated fleet",
    "one fleet's pool; the adoption rule's constellation",
)

MIXED_FLEETS = Constellation(
    "two mixed fleets",
    "background and distractors split across two fleets whose systems overlap; "
    "each version pair spans the fleets, so a pair can never group",
)

ORG_BOUNDARY = Constellation(
    "org alongside fleet",
    "a quarter of the background and every newer statement org-scoped, the rest "
    "in one fleet; org entries group only with org entries (ADR 0074)",
)

CONSTELLATIONS: tuple[Constellation, ...] = (
    ORG_ONLY,
    ISOLATED_FLEET,
    MIXED_FLEETS,
    ORG_BOUNDARY,
)

# The constellation the adoption rule applies to.
ADOPTION_CONSTELLATION = ISOLATED_FLEET


def _assign(constellation: Constellation, role: str, index: int) -> tuple[str, str | None]:
    if constellation is ORG_ONLY:
        return ("org", None)
    if constellation is ISOLATED_FLEET:
        return ("fleet", FLEET_A)
    if constellation is MIXED_FLEETS:
        if role in ("background", "distractor"):
            return ("fleet", FLEET_A if index % 2 == 0 else FLEET_B)
        return ("fleet", FLEET_A if role == "old" else FLEET_B)
    if constellation is ORG_BOUNDARY:
        if role in ("background", "distractor"):
            return ("org", None) if role == "background" and index % 4 == 0 else ("fleet", FLEET_A)
        return ("org", None) if role == "new" else ("fleet", FLEET_A)
    return constellation.assign(role, index)


@dataclass(frozen=True, slots=True)
class Row:
    """One setting (``tau`` None = grouping off) on one domain, as counts so
    that domains pool by addition."""

    tau: float | None
    false_grouped: int = 0  # distractors grouped with a version member or a distractor
    distractors: int = 0
    pairs_grouped: int = 0
    pairs: int = 0
    rr_new: float = 0.0  # summed reciprocal ranks of the newer entry
    rr_old: float = 0.0
    hits: float = 0.0  # summed hit@K over every query
    queries: int = 0
    background_groups: int = 0  # groups touching the domain's own entries (information)

    @property
    def false_rate(self) -> float:
        return self.false_grouped / self.distractors if self.distractors else 0.0

    @property
    def true_rate(self) -> float:
        return self.pairs_grouped / self.pairs if self.pairs else 0.0

    @property
    def mrr_new(self) -> float:
        return self.rr_new / self.pairs if self.pairs else 0.0

    @property
    def mrr_old(self) -> float:
        return self.rr_old / self.pairs if self.pairs else 0.0

    @property
    def hit_at_k(self) -> float:
        return self.hits / self.queries if self.queries else 0.0

    def __add__(self, other: Row) -> Row:
        assert self.tau == other.tau
        return Row(
            self.tau,
            self.false_grouped + other.false_grouped,
            self.distractors + other.distractors,
            self.pairs_grouped + other.pairs_grouped,
            self.pairs + other.pairs,
            self.rr_new + other.rr_new,
            self.rr_old + other.rr_old,
            self.hits + other.hits,
            self.queries + other.queries,
            self.background_groups + other.background_groups,
        )


@dataclass(frozen=True, slots=True)
class Measurement:
    """The sweep on every domain: per domain, rows in sweep order with the
    grouping-off baseline first."""

    model: str
    domains: dict[str, list[Row]] = field(default_factory=dict)
    query_instruction: str = ""

    def pooled(self) -> list[Row]:
        rows = list(self.domains.values())
        return [sum(column[1:], column[0]) for column in zip(*rows, strict=True)]


@dataclass(frozen=True, slots=True)
class Verdict:
    tau: float
    failures: tuple[str, ...]

    @property
    def passes(self) -> bool:
        return not self.failures


def adoption(m: Measurement) -> list[Verdict]:
    """The adoption rule for every tau of the sweep: pooled false grouping at
    most 5 percent, no domain above 10 percent, the sysadmin domain passing
    (its own false grouping at most 5 percent and no loss against grouping
    off on newer-member MRR, old-exact MRR beyond 0.02, or hit@5); newer-member MRR up,
    old-exact MRR at most 0.02 below baseline, pooled hit@5 not lower."""
    pooled = m.pooled()
    base = pooled[0]
    verdicts = []
    for i, row in enumerate(pooled[1:], start=1):
        assert row.tau is not None
        failures = []
        if row.false_rate > POOLED_FALSE_MAX + EPS:
            failures.append(f"pooled false grouping {row.false_rate:.1%} > 5%")
        for name, rows in m.domains.items():
            if rows[i].false_rate > DOMAIN_FALSE_MAX + EPS:
                failures.append(f"{name} false grouping {rows[i].false_rate:.1%} > 10%")
        sysadmin = m.domains.get(SYSADMIN)
        if sysadmin is not None:
            s, s0 = sysadmin[i], sysadmin[0]
            if s.false_rate > POOLED_FALSE_MAX + EPS:
                failures.append(f"sysadmin false grouping {s.false_rate:.1%} > 5%")
            if (
                s.mrr_new < s0.mrr_new - EPS
                or s.mrr_old < s0.mrr_old - OLD_EXACT_TOLERANCE - EPS
                or s.hit_at_k < s0.hit_at_k - EPS
            ):
                failures.append("sysadmin loses against grouping off")
        if row.mrr_new <= base.mrr_new + EPS:
            failures.append("newer-member MRR not up")
        if row.mrr_old < base.mrr_old - OLD_EXACT_TOLERANCE - EPS:
            failures.append("old-exact MRR more than 0.02 below baseline")
        if row.hit_at_k < base.hit_at_k - EPS:
            failures.append("pooled hit@5 lower")
        verdicts.append(Verdict(row.tau, tuple(failures)))
    return verdicts


def suggest(m: Measurement) -> float | None:
    """The lowest tau that passes the rule (it groups the most true pairs),
    or None: keep grouping off."""
    passing = [v.tau for v in adoption(m) if v.passes]
    return min(passing) if passing else None


def _with_specific_queries(vdomain: VersionDomain) -> VersionDomain:
    """The same domain with each pair's ``query`` replaced by its specific
    (de-vagued) variant (`tests/eval/corpus/specific_queries`), so the tau
    sweep measures a refined agent retry. The entries are unchanged."""
    from tests.eval.corpus.specific_queries import SPECIFIC_PAIRS

    spec = SPECIFIC_PAIRS[vdomain.name]
    pairs = tuple(replace(p, query=spec[i]) for i, p in enumerate(vdomain.pairs))
    return replace(vdomain, pairs=pairs)


async def measure_domain(
    embedder: Embedder,
    vdomain: VersionDomain,
    sweep: tuple[float, ...] = SWEEP,
    constellation: Constellation = ORG_ONLY,
    query_style: str = "vague",
    query_instruction: str = "",
) -> list[Row]:
    """The grouping-off baseline, then one row per tau, on one domain,
    with the pool scoped per ``constellation``. ``query_style`` of
    "specific" swaps each pair's query for its de-vagued variant."""
    if query_style == "specific":
        vdomain = _with_specific_queries(vdomain)
    domain = vdomain.domain
    store = MemoryStore(make_clock())
    writer = WriteService(store, embedder)

    async def seed(entry: Entry, age_days: float, role: str, index: int) -> str:
        kind, summary, tags, body = entry
        scope, fleet_id = _assign(constellation, role, index)
        draft = EntryDraft(
            kind=Kind(kind),
            summary=summary,
            tags=tuple(tags),
            body=body,
            author="eval",
            agent="eval",
            occurred_at=FIXED_NOW - timedelta(days=age_days),
            scope=scope,
            fleet_id=fleet_id,
        )
        return (await writer.write(draft)).id

    background = [await seed(e, 0.0, "background", i) for i, e in enumerate(domain.entries)]
    background_set = set(background)
    old_ids, new_ids, distractor_ids = [], [], []
    for i, pair in enumerate(vdomain.pairs):
        old_ids.append(await seed(pair.old, 90 + 15 * i, "old", i))  # long ago
        new_ids.append(await seed(pair.new, 5 + 6 * i, "new", i))  # recent
        distractor_ids.append(await seed(pair.distractor, 20 + 7 * i, "distractor", i))
    version_members = {*old_ids, *new_ids}
    distractors = set(distractor_ids)

    own = [(q, {background[i] for i in rel}) for q, rel in domain.answerable]
    base_config = replace(
        make_search_config(candidate_top_k=20, default_limit=40),
        query_instruction=query_instruction,
    )
    rows = []
    for tau in (None, *sweep):
        config = replace(base_config, version_group_min_similarity=tau)
        service = SearchService(store, embedder, config, now_fn=lambda: FIXED_NOW)

        async def ranked(query: str, service: SearchService = service) -> list[Hit]:
            return await service.search(query, limit=40)

        false_grouped: set[str] = set()
        pairs_grouped = 0
        background_groups = 0
        rr_new = rr_old = hits = 0.0
        queries = 0
        for i, pair in enumerate(vdomain.pairs):
            for query, answer, is_current in (
                (pair.query, new_ids[i], True),
                (pair.old_query, old_ids[i], False),
            ):
                result = await ranked(query)
                order = [h.entry_id for h in result]
                relevant = {answer: 1}
                hits += hit_at_k(order, relevant, K)
                queries += 1
                if is_current:
                    rr_new += reciprocal_rank(order, relevant)
                else:
                    rr_old += reciprocal_rank(order, relevant)
                for hit in result:
                    for other in hit.similar:
                        a, b = hit.entry_id, other.entry_id
                        if {a, b} == {old_ids[i], new_ids[i]} and is_current:
                            pairs_grouped += a < b  # count the edge once
                        if (a in distractors and (b in distractors or b in version_members)) or (
                            b in distractors and a in version_members
                        ):
                            false_grouped.update(x for x in (a, b) if x in distractors)
                        if a in background_set or b in background_set:
                            background_groups += a < b
        for query, relevant_ids in own:
            order = [h.entry_id for h in await ranked(query)]
            hits += hit_at_k(order, dict.fromkeys(relevant_ids, 1), K)
            queries += 1
        rows.append(
            Row(
                tau,
                false_grouped=len(false_grouped),
                distractors=len(distractors),
                pairs_grouped=pairs_grouped,
                pairs=len(vdomain.pairs),
                rr_new=rr_new,
                rr_old=rr_old,
                hits=hits,
                queries=queries,
                background_groups=background_groups,
            )
        )
    return rows


async def measure(
    embedder: Embedder,
    domains: tuple[VersionDomain, ...] = VERSION_DOMAINS,
    sweep: tuple[float, ...] = SWEEP,
    constellation: Constellation = ORG_ONLY,
    query_style: str = "vague",
    query_instruction: str = "",
) -> Measurement:
    result = Measurement(embedder.model_name, query_instruction=query_instruction)
    for vdomain in domains:
        result.domains[vdomain.name] = await measure_domain(
            embedder, vdomain, sweep, constellation, query_style, query_instruction
        )
    return result


async def measure_all(
    embedder: Embedder,
    domains: tuple[VersionDomain, ...] = VERSION_DOMAINS,
    constellations: tuple[Constellation, ...] = CONSTELLATIONS,
    sweep: tuple[float, ...] = SWEEP,
    query_style: str = "vague",
    query_instruction: str = "",
) -> dict[str, Measurement]:
    """The sweep on every constellation; the keys are constellation names."""
    return {
        c.name: await measure(embedder, domains, sweep, c, query_style, query_instruction)
        for c in constellations
    }


def _table(rows: list[Row]) -> list[str]:
    lines = [
        f"| tau | false grouping | true grouping | newer-member MRR | old-exact MRR | hit@{K} "
        "| groups with other entries |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        tau = "off" if r.tau is None else f"{r.tau:.2f}"
        lines.append(
            f"| {tau} | {r.false_grouped}/{r.distractors} ({r.false_rate:.0%}) "
            f"| {r.pairs_grouped}/{r.pairs} ({r.true_rate:.0%}) | {r.mrr_new:.3f} "
            f"| {r.mrr_old:.3f} | {r.hit_at_k:.3f} | {r.background_groups} |"
        )
    return lines


def _instruction_label(instruction: str) -> str:
    return f"`{instruction}`" if instruction else "none"


def report(m: Measurement) -> str:
    """The measurement as Markdown, ready for docs/retrieval-experiments.md."""
    lines = [
        f"Model: `{m.model}`. Query instruction: {_instruction_label(m.query_instruction)}.",
        "",
    ]
    for name, rows in m.domains.items():
        lines += [f"### {name}", "", *_table(rows), ""]
    lines += ["### Pooled", "", *_table(m.pooled()), ""]
    lines += ["### Adoption rule", ""]
    lines += [
        "Adopt a tau only where pooled false grouping is at most 5%, no domain is above "
        "10%, the sysadmin domain passes (false grouping at most 5%, no loss against off, "
        "old-exact within 0.02), newer-member MRR is up, old-exact MRR is at most 0.02 below "
        "baseline and pooled hit@5 is not lower.",
        "",
    ]
    for v in adoption(m):
        lines.append(f"* {v.tau:.2f}: " + ("passes" if v.passes else "; ".join(v.failures)))
    best = suggest(m)
    lines += [
        "",
        "Suggested HIVEMIND_VERSION_GROUP_MIN_SIMILARITY: "
        + (f"{best:.2f}" if best is not None else "none (keep grouping off)"),
    ]
    return "\n".join(lines)


def report_all(results: dict[str, Measurement]) -> str:
    """The sweep on every constellation, as Markdown.

    The adoption rule applies to ``ADOPTION_CONSTELLATION`` only; the other
    constellations are sensitivity readings (the fleet rule changes what can
    group, not the similarities).
    """
    lines: list[str] = []
    for name, m in results.items():
        if not lines:
            lines += [f"Query instruction: {_instruction_label(m.query_instruction)}", ""]
        lines += [f"## Constellation: {name}", ""]
        if name == ADOPTION_CONSTELLATION.name:
            lines += [report(m), ""]
        else:
            note = next(c.note for c in CONSTELLATIONS if c.name == name)
            lines += [f"{note}.", ""]
            for dom, rows in m.domains.items():
                lines += [f"### {dom}", "", *_table(rows), ""]
            lines += ["### Pooled", "", *_table(m.pooled()), ""]
    return "\n".join(lines)


async def _main() -> None:
    import os

    from hivemind.config import endpoint_label, load_settings
    from hivemind.embeddings import EmbeddingError, build_embedder
    from tests.eval.corpus import EXTENDED_VERSION_DOMAINS
    from tests.eval.threshold import CachingEmbedder

    settings = load_settings()
    query_style = os.environ.get("EVAL_QUERY_STYLE", "vague").strip().lower()
    query_instruction = os.environ.get("EVAL_QUERY_INSTRUCTION", "").strip()
    try:
        embedder = CachingEmbedder(build_embedder(settings))
        results = await measure_all(
            embedder,
            EXTENDED_VERSION_DOMAINS,
            query_style=query_style,
            query_instruction=query_instruction,
        )
        print(report_all(results))
    except EmbeddingError as exc:
        where = endpoint_label(settings.embedding_endpoint)
        raise SystemExit(f"embedder at {where} unavailable ({exc}); start it (make vllm)") from None


if __name__ == "__main__":
    asyncio.run(_main())
