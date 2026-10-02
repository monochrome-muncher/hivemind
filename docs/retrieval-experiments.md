# Retrieval experiments

The measurements behind ROADMAP §4.4 and §4.6, moved here verbatim from
the ROADMAP so it can stay a plan. They are point-in-time results on a
synthetic, age-varied fixture (`tests/eval/temporal.py`, 16 entries, 10
queries, a 4-dimension hash embedder), not pinned gates. The decision
they led to is [ADR 0022](adr/0022-recency-floor-so-match-quality-is-the-sort-key.md).

## §4.4 Gate the recency term to temporal queries

*(measured —
**gating rejected, no code change**; fixture + runner:
`tests/eval/temporal.py`, `tests/eval/test_decay_experiment.py`)*
`entry_score` (`retrieval/scoring.py`) multiplies every hit by
`0.5 ** (age_days / half_life_days)` — **unconditionally**, on every
query. An always-on recency term is a known way to depress recall on
non-temporal queries: an old, exactly-right entry loses to a recent,
vaguely-related one even when the query carries no time sense at all.
*(Prior art: an external system measured this exact regression and
moved to a gated, additive recency term; that is a hypothesis to test
here, not a result to copy.)*

**The golden set could not answer this.** Every §1.1 golden entry is
seeded with no `occurred_at`, so all eight share one timestamp, the
recency factor is identical for every candidate and cancels out of the
ranking. Measuring decay needed a second, age-varied fixture
(`tests/eval/temporal.py`: 16 entries aged 2–500 days, 10 queries each
labelled temporal / non-temporal). The pinned §1.1 gate is untouched.

**Measured** (k=5; variants realised through `SearchConfig.half_life_days`
only, so the fused RRF score is identical across all three and every
difference is attributable to the recency factor):

| variant | non-temporal hit@5 / MRR / nDCG@5 | temporal hit@5 / MRR / nDCG@5 | all |
|---|---|---|---|
| **A** always-on (today) | 0.333 / 0.208 / 0.238 | 0.750 / 0.625 / 0.658 | 0.500 / 0.375 / 0.406 |
| **B** off | 1.000 / 0.917 / 0.938 | 1.000 / 0.708 / 0.783 | 1.000 / 0.833 / 0.876 |
| **C** gated (oracle label) | 1.000 / 0.917 / 0.938 | 0.750 / 0.625 / 0.658 | 0.900 / 0.800 / 0.826 |

The hypothesis is **confirmed** (A's non-temporal MRR 0.208 vs. 0.917
without the term) but the **proposed fix is refuted**: C loses to B on
every aggregate (all-query MRR 0.800 vs. 0.833, hit@5 0.900 vs. 1.000),
and C's numbers are an *oracle* ceiling — they use the fixture's
hand-written label, not a classifier the service has. Sliced by
competition pattern instead of by query time sense: on "old entry
matches almost verbatim, recent entry shares a phrase" A scores
**0.000** hit@5 and B **1.000**, for the temporal and the non-temporal
query alike — so time sense is not the axis that predicts where the
term helps, and gating on it only narrows where the failure shows up.

**Root cause, and it is not fixture-dependent.** With the SPEC §6.2
defaults (`rrf_k=60`, `w=0.5/0.5`, `candidate_top_k=20`) the fused
score spans at most 2.62x across a candidate list (1.31x when both
candidates appear in both streams). The recency factor spans 2x *per
half-life*. So an entry ~42 days older than a competitor is outranked
by it no matter how much better it matches — 12 days when both are in
both streams. Multiplicative decay against RRF's compressed range is
not a tie-break, it is the sort key.

**Resolved: no change to `scoring.py`.** Gating is rejected on the
numbers — that conclusion stands and is not reopened here. The
residual finding, that the *form* of §6.4's recency term is mismatched
to RRF's range independent of gating, is carried forward as its own
tracked item: **§4.6**.

*(The table above is a point-in-time measurement, not a pinned gate —
it reflects the repo as of commit `f65a4e0` and can drift if the eval
fixture or scoring config changes without a re-run.)*

## §4.6 The form of the recency term is mismatched to RRF's range

*(**direction (a) is RESOLVED and SHIPPED — ADR 0022**: the recency
factor is floored, `SearchConfig.recency_floor = 0.8` by default, and
SPEC §6.4 now carries the floored formula. That **rules out direction
(b)**, the additive form — per this section the two are competing
answers to the same defect and must not be stacked; reopening (b)
means *replacing* the floor under a new ADR. **Per-kind half-lives
stays HELD**: orthogonal, not a fix for this finding. The `rrf_k`
sweep came back negative and changed no default. What remains open
here is the *value* 0.8, not the form — see the re-measure trigger at
the end of this item.)* §4.4 measured
and rejected *gating* the recency term. Underneath that result sits a
separate, arithmetic mismatch that gating would not have fixed either
way: with the SPEC §6.2 defaults the fused RRF score spans at most
**2.6230x** across a candidate list, while `entry_score`'s
`0.5 ** (age_days / half_life_days)` (SPEC §6.4) spans **2x per
30-day half-life** — so **~41.7 days** of age difference (**11.7
days** when both candidates appear in both retrieval streams)
outranks any match-quality difference, however large. This is
arithmetic on the two formulas, not a property of the §4.4 fixture —
it holds for any candidate list, synthetic or real, at the §6.2
defaults.

**Measure `rrf_k` FIRST — ahead of the floor and the additive form.**
The mismatch has *two* sides, and everything above only ever looked at
the recency side. `rrf_k=60` is the side doing the **compressing**.
With the SPEC §6.2 defaults, the fused score of the best possible
candidate (rank 1 in **both** streams, `w=0.5/0.5`) is `1/(k+1)` and
the worst retained candidate (rank 20 in **one** stream,
`candidate_top_k=20`) is `0.5/(k+20)`, so the whole fused range is

    2(k + 20) / (k + 1)

| `rrf_k` | fused range | = half-lives of age | = days at a 30-day half-life |
|---|---|---|---|
| 60 (today) | 2.62x | 1.39 | **41.7** |
| 20 | 3.81x | 1.93 | 57.9 |
| 10 | 5.45x | 2.45 | 73.4 |
| 5 | 8.33x | 3.06 | 91.8 |

Lowering `k` widens the fused range, and every doubling of that range
buys exactly one more half-life before age outranks match quality.
It is a **pure `SearchConfig` value**: no SPEC change, no ADR, no new
code, reversible in one line — where (a) and (b) are both §6.4
redesigns. Measuring the free knob before the expensive ones is the
order this item is now written in; the measurement itself is §4.6's
table below.

**The coupling, which nobody had noted:** a *lower* `k` does not
only help. RRF's `w/(k+rank)` gets steeper as `k` shrinks, so a low
`k` **amplifies whichever stream orders badly** — it puts more of the
fused score on that stream's top one or two positions. Per §4.1,
`ts_rank` is the weaker ranker (no IDF at all: a rare discriminating
term is weighted like a common one). So `rrf_k` and the BM25 question
are **coupled**: the case for a low `k` is strongest when both
streams order well, and lowering `k` raises the price of
`ts_rank`'s mistakes. A `k` chosen on today's keyword stream should
be re-checked if §4.1 ever lands.

Two further directions were identified, neither measured yet:
**(a)** bound the term hard enough that it can no longer dominate the
fused range — which, at that tightness, is close to removing it — or
**(b)** move to the additive form the prior art (§4.4) used, so
recency competes on the same scale as the fused score instead of
multiplying it. Landing either is a SPEC §6.4 redesign, not a scoring
tweak, and gets its own ADR, not a quiet edit of `scoring.py`.

**These are NOT a sequence — do not work them in order.** `rrf_k` is
a config change and comes first because it is free and reversible.
**(a) and (b) are competing forms of the same fix: pick one, never
both** — a floored multiplicative term and an additive term are two
answers to "stop recency being the sort key", and stacking them just
makes the term untunable. *Per-kind half-lives* (a `fact` and a
`decision` decaying at different rates) is a third, **orthogonal**
idea — it changes which entries decay, not how decay competes with
match quality — and it **stays held**: it is not a fix for this
finding and would only add a knob on top of an already-mismatched
form.

**`rrf_k` MEASURED — and the answer is no.** *(fixture +
runner: `tests/eval/temporal.py`, `tests/eval/temporal_runner.py`,
`tests/eval/test_rrf_k_experiment.py`; `SearchConfig.rrf_k` is
**unchanged** at 60 and the §1.1 gate is untouched)* The sweep runs
`rrf_k` over {60, 20, 10, 5} with **decay ON** (§4.4's variant A) —
everything else held fixed and shared with the §4.4 experiment:

| `rrf_k` | non-temporal hit@5 / MRR / nDCG@5 | temporal | `old_exact` | all |
|---|---|---|---|---|
| **60** (today) | 0.333 / 0.208 / 0.238 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 0.500 / 0.375 / 0.406 |
| **20** | 0.500 / 0.242 / 0.303 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 0.600 / 0.395 / 0.445 |
| **10** | 0.500 / 0.264 / 0.322 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 0.600 / 0.408 / 0.456 |
| **5** | 0.500 / 0.292 / 0.344 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 0.600 / 0.425 / 0.469 |
| *(60, decay off — §4.4's B, for reference)* | 1.000 / 0.917 / 0.938 | 1.000 / 0.708 / 0.783 | *1.000 / 1.000 / 1.000* | 1.000 / 0.833 / 0.876 |

**The question was: does lowering `rrf_k` recover the recall the
unconditional recency term destroys, without a change to
`scoring.py`? It does not.** The `old_exact` slice — the pattern
§4.4 identified as the real failure mode — stays at **0.000 hit@5 at
every `k`**, while decay-off answers both of those queries perfectly.
The relevant entries never enter the top 5 at any `k` in the sweep.

**And that is arithmetic, not a fixture artifact.** `2(k+20)/(k+1)`
is bounded above by **40x** (its limit as `k` -> 0), so the *entire*
`rrf_k` lever — from today's 60 all the way down to a degenerate `k`
nobody would ship — is worth at most **log2(40) = 5.3 half-lives**,
~160 days at the 30-day default. The `old_exact` entries are 420 and
380 days old: **14 and 12.7 half-lives**. No value of `rrf_k` closes
a gap that size, for this corpus or any other. **`rrf_k` is not a
cheap substitute for fixing the form of the term** — this is the
single most useful thing the sweep establishes, and it is the reason
§4.6 still needs (a) or (b).

**What `rrf_k` *does* buy here** (so the verdict is not overstated):
all-query MRR rises monotonically as `k` falls (0.375 -> 0.395 ->
0.408 -> 0.425) and non-temporal hit@5 improves 0.333 -> 0.500 at
`k`<=20. The gain is entirely on the non-temporal / timeless side;
the `temporal` and `currency_pair` slices are **bit-identical across
the whole sweep**, so nothing is being traded away for it on this
fixture.

**Verdict on `k=10` as a default: NOT SUPPORTED by this evidence —
and the evidence is too weak to support any new default.** The
direction is mildly favourable and the sweep shows no downside here,
but the differences are a handful of rank positions over 10
hand-written queries scored by a **4-dimension hash embedder**
(`tests/fakes.FakeEmbedder`) — which is exactly the evidence §4.4
refused to land a change on, and the numbers do not separate `k=10`
from `k=5` or `k=20` in any case. What the fixture *can* establish
(the bound above) argues the opposite of the hypothesis that
motivated the sweep. **`SearchConfig.rrf_k` stays at 60**; changing
it is a decision on real-corpus evidence, not a consequence of this
table.

**(a) THE FLOOR, MEASURED — and this one clears the bar.** *(seam
shipped **default-off**: `entry_score(..., recency_floor=...)` +
`SearchConfig.recency_floor: float | None = None`, validated to
`(0, 1]`, threaded through `SearchService`. The **default is
unchanged** — `None` is bit-for-bit today's behaviour, asserted at
both the pure-function and the eval seam. Sweep:
`tests/eval/test_decay_experiment.py::TestRecencyFloorSweep`; the
§1.1 gate and `tests/eval/golden.py` are untouched.)*

Where `rrf_k` widens the *fused* range against an unbounded recency
factor, a floor **bounds the unbounded factor itself**:
`recency = max(floor, 0.5 ** (age/half_life))`, so the recency range
collapses from `2 ** (age spread / half_life)` to exactly `1/floor`.
That has no analogue of the 40x cap. The threshold is derivable from
the pipeline's own constants *in advance*: match quality outranks age
once `1/floor` is narrower than the fused range, i.e.
**`floor > 1/2.6230 = 0.381`** at `rrf_k=60`.

Measured at `rrf_k=60`, decay ON, k=5 (A and B repeated from §4.4 as
the two limits of the same family — `floor -> 0` is A, `floor = 1`
is B):

| variant | non-temporal hit@5 / MRR / nDCG@5 | temporal | `old_exact` | `currency_pair` | `timeless` | all |
|---|---|---|---|---|---|---|
| **A** always-on (today) | 0.333 / 0.208 / 0.238 | 0.750 / 0.625 / 0.658 | **0.000 / 0.000 / 0.000** | 1.000 / **0.833** / 0.877 | 0.400 / 0.250 / 0.286 | 0.500 / 0.375 / 0.406 |
| **B** decay off | 1.000 / 0.917 / 0.938 | 1.000 / 0.708 / 0.783 | 1.000 / 1.000 / 1.000 | 1.000 / **0.611** / 0.710 | 1.000 / 0.900 / 0.926 | 1.000 / 0.833 / 0.876 |
| **D** floor 0.2 | 0.500 / 0.242 / 0.303 | 0.750 / 0.625 / 0.658 | 0.500 / 0.100 / 0.193 | 1.000 / 0.833 / 0.877 | 0.400 / 0.250 / 0.286 | 0.600 / 0.395 / 0.445 |
| **D** floor 0.381 | 0.500 / 0.242 / 0.303 | 0.750 / 0.625 / 0.658 | 0.500 / 0.100 / 0.193 | 1.000 / 0.833 / 0.877 | 0.400 / 0.250 / 0.286 | 0.600 / 0.395 / 0.445 |
| **D** floor 0.5 | 1.000 / 0.556 / 0.665 | 1.000 / 0.708 / 0.783 | 1.000 / 0.500 / 0.631 | 1.000 / 0.778 / 0.833 | 1.000 / 0.567 / 0.672 | 1.000 / 0.617 / 0.712 |
| **D** floor 0.7 | 1.000 / 0.556 / 0.665 | 1.000 / 0.833 / 0.875 | 1.000 / 0.750 / 0.815 | 1.000 / 0.778 / 0.833 | 1.000 / 0.567 / 0.672 | 1.000 / 0.667 / 0.749 |
| **D** floor **0.8** | 1.000 / 0.653 / 0.738 | 1.000 / 0.833 / 0.875 | **1.000 / 1.000 / 1.000** | 1.000 / **0.778** / 0.833 | 1.000 / 0.583 / 0.686 | 1.000 / 0.725 / 0.793 |
| **D** floor 0.9 | 1.000 / 0.833 / 0.877 | 1.000 / 0.688 / 0.765 | 1.000 / 1.000 / 1.000 | 1.000 / **0.583** / 0.687 | 1.000 / 0.800 / 0.852 | 1.000 / 0.775 / 0.832 |

**The discriminating question — does a floor recover the `old_exact`
cases A scores 0.000 on, *while keeping* the `currency_pair` cases B
loses? Yes, and 0.8 is the value.** At floor 0.8 `old_exact` goes
0.000 -> **1.000** MRR (the relevant entry is rank 1 for both
queries, matching B exactly) while `currency_pair` holds at
**0.778** — above B's 0.611, below A's 0.833. Floors 0.5 and 0.7
clear the same bar less completely (`old_exact` MRR 0.500 / 0.750,
same 0.778 on currency pairs). **This is the first variant anything
in §4.4 or §4.6 has produced that beats *both* extremes on the slice
each is weak at** — gating (C) did not, and no `rrf_k` did.

**A floor is a band, not a slider.** At 0.9 the recency range is
1.11x — narrower than the fused spread of almost any pair — so the
variant is nearly B again and pays B's price: `currency_pair` MRR
drops to 0.583, *below* decay-off's 0.611. Push the floor to 1.0 and
it **is** B. That non-monotonicity is why this reports a value, not
a direction.

**The arithmetic predicted this before the queries ran, and the
measurement confirms the prediction's shape.** Below the derived
0.381 threshold (floors 0.2 and 0.381) `old_exact` MRR stays at
0.100 — the entry surfaces at rank 5 at best. The recovery begins
just above it and completes by 0.8. The threshold is a *lower bound*
on where the effect can start, not where it finishes: two candidates
that both appear in **both** streams span far less than the
best-vs-worst 2.62x, so flipping those needs a tighter floor. 0.381
predicts the onset; 0.8 is where the fixture says it is done.

**Why this evidence is stronger than the `rrf_k` table** (it is the
same 10 hand-written queries over 16 entries scored by the same
4-dimension hash embedder, and that has not changed): A, B and D
differ **only** by a bounded rebalance of the *same* fused scores
from the *same* embedder — no stream is re-ranked, no candidate set
moves, the fused values are bit-identical — and the direction and
approximate threshold were derived from the two formulas *before*
the sweep. What the fixture still **cannot** establish: that 0.8 is
the optimum on a real corpus (the band 0.5–0.8 is barely separated
here, and `timeless`/`non_temporal` MRR keeps rising past 0.8 while
`currency_pair` falls, so the optimum is a *trade*, not a peak the
4-dim embedder can locate); that real queries distribute over these
competition patterns the way this fixture does; or that a real
embedder's vector stream would place the same candidates in the same
streams at all. It establishes **a mechanism that works and the
sign of its effect**, not a tuned constant.

**Verdict: SHIPPED — ADR 0022 flipped the default to `0.8`.** The
seam landed behaviour-preserving in `b61db0f`; the decision to turn
it on was taken on these numbers by a human and is recorded in
**[ADR 0022](adr/0022-recency-floor-so-match-quality-is-the-sort-key.md)**,
which amends SPEC §6.4. Three things that ADR insists on and this
table should not be read without: the floor is a **band, not a
slider** (operators must not tune it up — 0.9 measures *worse* than
decay-off on `currency_pair`); it is **not free** (`currency_pair`
MRR 0.833 → 0.778, traded for `old_exact` 0.000 → 1.000); and on the
all-query aggregate decay-off still wins on this fixture (0.833 vs.
0.725) — the floor is chosen to keep the currency slice, not to win
that average. Landing (a) **rules out (b)**: per this section, (a)
and (b) are competing forms of the same fix and must not be stacked.

**Still open in (a): the value, not the form.** 0.8 comes from 16
synthetic entries and 10 queries scored by a 4-dimension hash
embedder. The *arithmetic* (`1/floor` against the 2.6230x fused
range) is fixture-independent; the constant is not. **Re-measure
trigger:** the first real corpus with meaningful age spread — run the
§1.1 harness against real queries on an aged production pool and
sweep the 0.5–0.9 band. Expect to move the value, not the form.

The §1.1 gate was **not** re-pinned for this: every golden entry is
seeded without `occurred_at`, so all eight share one timestamp, every
recency factor is 1.0 and a lower bound on 1.0 is a no-op. The gate's
report is bit-identical with and without the floor, asserted in
`test_eval_gate.py::...::test_the_shipped_recency_floor_cannot_move_this_gate`.

**Trigger:** for **(b)** and for any `rrf_k` default change,
unchanged — the first real corpus with meaningful age spread, i.e.
run the §1.1 harness against real queries on an aged pool once there
is production usage. Not before: 10 synthetic queries scored by a
4-dimension hash embedder is exactly the wrong evidence to land a
scoring change on (the same reasoning §4.4 closed on). What *is*
settled without that trigger is the bound: `rrf_k` cannot substitute
for (a) or (b), so the cheap knob is now measured and out of the
way. **(a) is closed (ADR 0022)**, so its trigger no longer gates
anything: the floor is on at 0.8 and the real corpus is what should
*re-check* that number within the 0.5–0.9 band, not what unblocks the
idea. Note that (b) is now ruled out by (a) having landed — its
trigger firing means reconsidering the floor itself, under a new ADR,
not adding an additive term on top of it.

## Similarity threshold

*(ADR 0062; tool: `tests/eval/threshold.py`, `make measure-threshold`)*
`HIVEMIND_VECTOR_MIN_SIMILARITY` is off by default. When set, the vector
stream keeps only entries whose cosine similarity to the query is above
it, so a search with nothing relevant can come back empty and shows up
in the empty-search counter (ADR 0056). The keyword stream is not
thresholded.

**Why it is measured per deployment.** Similarities depend on the model
and on its dimension: one model puts related text at 0.8 and unrelated
text at 0.4, another at 0.6 and 0.1. The hash embedder the other
experiments use cannot stand in for a real model here.

**Measured: 0.60 for the Qwen3-Embedding family at 1024 dimensions.**
*(2026-10-02; `tests/eval/threshold.py` on a scratch Postgres pool, so
the "search empty" column uses Postgres keyword matching. Models run
through llama.cpp with last-token pooling, truncated to 1024 dimensions
and re-normalised as a `dimensions` request does: 8B as the Q8_0 GGUF,
4B and 0.6B as f16. Embedded as the service does, with no query
instruction.)*

| model | weakest answer | strongest off-topic | suggested |
|---|---|---|---|
| Qwen3-Embedding-8B | 0.655 | 0.617 | 0.60 |
| Qwen3-Embedding-4B | 0.665 | 0.623 | 0.60 |
| Qwen3-Embedding-0.6B | 0.732 | 0.679 | 0.65 |

All three separate the answerable from the off-topic queries, with a
narrow gap: the off-topic queries are mostly from the same domain. The
aggregate below weights 8B 70%, 4B 20% and 0.6B 10%; per-model
suggestions weighted the same way give 0.605.

| threshold | answers kept in vector stream | hit@5 | off-topic: vector stream empty | off-topic: search empty |
|---|---|---|---|---|
| 0.45 | 100% | 1.000 | 3% | 3% |
| 0.50 | 100% | 1.000 | 33% | 33% |
| 0.55 | 100% | 1.000 | 40% | 37% |
| **0.60** | **100%** | **1.000** | **72%** | **58%** |
| 0.65 | 100% | 1.000 | 98% | 73% |
| 0.70 | 95% | 1.000 | 100% | 75% |
| 0.80 | 94% | 1.000 | 100% | 75% |
| 0.90 | 51% | 1.000 | 100% | 75% |

At 0.60 every answer stays in the vector stream and seven in twelve
off-topic searches come back empty. The other off-topic queries that
pass the vector side are still caught by the keyword stream sharing a
word ("rotation", "size", "locking" matching "lock"), which no threshold changes.
hit@5 stays at 1.000 throughout because the keyword stream carries the
answers the vector stream drops; on queries that share no word with
their answer it would not. 0.65 empties more but leaves 8B a margin of
0.005 over its weakest answer. These are 18 synthetic queries: treat
0.60 as the starting value and tune it with the counters below.

**How to measure.** Point the `HIVEMIND_EMBEDDING_*` settings at the
embedder the deployment uses, with the deployment's dimension, and run:

    make vllm   # or use the production endpoint
    make measure-threshold HIVEMIND_EMBEDDING_DIM=1024

The tool embeds the golden and age-varied corpora (24 entries) the way
writes do, then runs 18 queries each answered by one of them and 12
off-topic queries answered by none (mostly in the same domain, so they
are hard cases). Nothing touches Postgres. For each threshold from 0 to
0.9 it reports:

| column | meaning |
|---|---|
| answers kept in vector stream | answerable queries whose answer is still above the threshold |
| hit@5 | answerable queries with their answer in the top 5, through the full pipeline |
| off-topic: vector stream empty | off-topic queries the threshold leaves with no vector match |
| off-topic: search empty | off-topic queries with no hit at all. Keyword matching here is `MemoryStore`'s, which counts any shared word, stop words included, so Postgres comes back empty at least as often |

It also prints the lowest similarity of an answer and the highest of an
off-topic query, and a suggested value: the highest 0.05 step at least
0.05 below the weakest answer, so every answer stays in the vector
stream. A missed answer costs an agent more than an unrelated hit.

**How to tune it later.** The synthetic sets are small, so the
suggestion is a starting point. Once it is set, watch two things: the
share of empty searches per fleet (`hivemind_searches_empty` against
`hivemind_searches` on `/metrics`) and `stale`/`wrong` feedback or
agents reporting that a search missed something it should have found.
Few empty searches and unrelated hits mean the value can go up by 0.05;
agents missing entries they wrote means it is too high. Measure again
after changing the embedding model or dimension; leaving the setting
unset restores the old behaviour at once.
