# Retrieval experiments

The measurements behind ROADMAP §4.4 and §4.6, moved here verbatim from
the ROADMAP so it can stay a plan. They are point-in-time results on a
synthetic, age-varied fixture (`tests/eval/temporal.py`, 16 entries, 10
queries, a 4-dimension hash embedder), not pinned gates. The decision
they led to is [ADR 0022](adr/0022-recency-floor-so-match-quality-is-the-sort-key.md).
`make measure-experiments` re-runs the decay, recency-floor and `rrf_k`
sweeps below and prints their tables; the default test run keeps only their
guards.

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
stays HELD** (since built, off by default, ADR 0072; removed, ADR 0103): orthogonal, not a fix for this finding. The `rrf_k`
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
match quality — and it **stays held** (since built, off by default, ADR 0072; removed, ADR 0103): it is not a fix for this
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

## Social signals: baselines for the capped budget (ADR 0070)

ADR 0070 puts the upward effect of all social signals under one cap
(`social_max`, 1.2) and adds invariants every ranking change must pass.
Nothing here is a gate; the numbers pin today's direction and arithmetic
so a later change flips an outcome on purpose. No embeddings are used.

**Scenarios S1-S4** (`tests/eval/social.py`, `test_social_scenarios.py`),
shipped config, importance 3, equal match unless stated. S1-S3 scores
are floor (0.8, shared by those candidates) × social factor; importance 3
is neutral under the narrowed factor (ADR 0088).

| id | candidates | today | winner | meant to flip with |
|---|---|---|---|---|
| S1 | 400-day entry, 3 helpful vs unlinked 20-day correction, no feedback | 0.896 vs 0.800 | old entry | supersession of the older version |
| S2 | two-year-old fact, 1 helpful re-checked yesterday vs never checked | 0.853 vs 0.800 | re-checked | a stronger social signal for a re-checked old fact (the confirmation clock was removed, ADR 0103) |
| S3 | four-year-old entry, 4 helpful + 1 fresh stale vs unrated, same age | 0.846 vs 0.800 | flagged entry | reserve "helpful evaporation" |
| S4 | importance 5, 16th in both streams vs importance 3, 1st in both | 0.012895 vs 0.014605 (score at 5 days old) | exact match | flipped by narrowed importance (ADR 0088); the wide factor gave the loose match, 0.013158 vs 0.013115 (fused × importance factor) |

S2 is barely separated: a recheck yesterday is worth the same 1.067 as
one made two years ago (the removed confirmation clock made it 0.960
against 0.800, see the next section). S4's fused scores are `Σ 0.5 / (60 + rank)` over
the two streams.

**Aged currency pairs** (`tests/eval/test_aged_currency.py`). The fixture's
currency pairs (entries 4/5, 6/7, 8/9) have the older member at 300-400
days and the newer at 5-12 days. Here the newer member is moved to 30,
60 and 120 days and the older keeps its age, so both are past the 9.7-day
point and carry the same recency factor. The slice is the 3 currency-pair
queries, newer member relevant, k = 5.

| newer member | config | hit@5 | MRR |
|---|---|---|---|
| 5-12 days (as written) | shipped | 1.000 | 0.778 |
| 5-12 days (as written) | decay off | 1.000 | 0.611 |
| 30, 60 or 120 days | shipped | 1.000 | 0.583 |
| 30, 60 or 120 days | decay off | 1.000 | 0.611 |

The three aged rows are identical because the floor makes recency flat
past 9.7 days. With the newer member that old, recency no longer helps
it, and the shipped config is slightly behind decay off: the floor still
lifts the fresh, unrelated entries over both members. The currency pairs
are the case a confirmation date restarting the recency clock should
improve.

## A confirmation restarts the recency clock (ADR 0071)

> **Removed (ADR 0103).** The confirmation clock was re-measured on 2026-10-21 with the Qwen3 embedders, came out worse, and was taken out. The results below are kept as history; `tests/eval/confirmation.py` is now `tests/eval/ranking.py` and no longer measures it.

*(tools: `tests/eval/confirmation.py`, `make measure-confirmation`; the
embedding-free slices in `test_social_scenarios.py` and
`test_aged_currency.py`)* `HIVEMIND_RECENCY_FROM_CONFIRMATION` is off by
default. With it on, the social factor is `min(social_max, quality ×
r_conf / r_occ)`, where `r_conf` ages the entry from its "last confirmed"
date. Everything below is the hash embedder or no embedder at all: it
checks direction and arithmetic, and the real-embedder run decides.

**S2** (two-year-old fact, 1 helpful, re-checked yesterday vs never
checked, shipped config, floor divided out):

| setting | re-checked | never checked | margin |
|---|---|---|---|
| off | 0.853 | 0.800 | 0.053 |
| on | 0.960 | 0.800 | 0.160 |

The gain is 1.0667 × 0.977 / 0.8 = 1.30, held at 1.2 by the cap, so 0.8 ×
1.2 = 0.96. The winner is the same; the margin is three times as wide.

**Aged currency pairs** (the 3 currency-pair queries, newer member relevant,
k = 5), with a helpful-with-a-note report one day old on each newer member.
The report lifts quality with the setting on or off, so compare the two
settings on the same rows; the no-feedback rows are from the section above.

| newer member | no feedback, shipped | confirmed, setting off | confirmed, setting on |
|---|---|---|---|
| 5-12 days (as written) | hit@5 1.000, MRR 0.778 | 1.000, 0.778 | 1.000, 0.833 |
| 30, 60 or 120 days | 1.000, 0.583 | 1.000, 0.778 | 1.000, 0.833 |

**Old-exact and the rest, with confirmations sprinkled on 20%** of the 16
entries of the age-varied fixture, each dated a day ago, setting on against
off over the same rows (MRR; hit@5 is 1.000 throughout). The 20% is the 3
entries (18.75%) whose summary has the smallest SHA-256, chosen by a
deterministic hash so it is independent of position and of any query:
entries 3, 7 and 14.

| slice | off | on | change |
|---|---|---|---|
| old_exact (2 queries) | 1.000 | 1.000 | 0.000 |
| non_temporal (6) | 0.681 | 0.597 | -0.084 |
| timeless (5) | 0.617 | 0.517 | -0.100 |
| currency_pair (3) | 0.778 | 0.833 | +0.056 |
| all (10) | 0.742 | 0.708 | -0.034 |

Old-exact is within the 0.02 rule for this pick, but not robustly. The
same comparison with every fifth entry confirmed, from offset 0 to 4 (4, 3,
3, 3 and 3 entries), gives old_exact off to on of 0.750 to 0.750, 0.750 to
0.750, 1.000 to 1.000, 1.000 to 0.500 and 0.667 to 0.333, and `all` falling
by 0.013, 0.050, 0.083, 0.164 and 0.125. The cause is the intended
mechanism and its risk: a confirmed recent distractor, or a confirmed
older entry on the same topic, gains up to 1.25x on an old exact match that
stays at the floor, in a 16-entry fixture where fused scores are close. On
this fixture the pass rule is **not** met; it says nothing yet about the
real embedders, whose match scores separate more, which is why the setting
stays off.

**The measurement entry point.** `make measure-confirmation` seeds each
domain of `tests/eval/domains.py` and the pool of all five with entries aged
0 to 400 days (a deterministic golden-ratio spread), sprinkles
confirmations on the 20% with the smallest summary hash (helpful with a
note, from a user who is not the author, 0.5 to 5 days old), and reports
hit@5 and MRR per variant with the changes against the baseline. Variants
are named sets of `SearchConfig` overrides, so later changes (a search
recency hint) are added to `VARIANTS` (per-kind recency is, ADR 0072). Hash-embedder
output, **machinery only, not evidence** (4 dimensions, the vector stream is
close to noise):

| fixture | hit@5 off | hit@5 on | MRR off | MRR on |
|---|---|---|---|---|
| all domains together | 0.458 | 0.365 | 0.244 | 0.208 |
| data analysis | 0.632 | 0.632 | 0.366 | 0.306 |
| software engineering | 0.684 | 0.632 | 0.486 | 0.467 |
| system administration | 0.684 | 0.684 | 0.412 | 0.382 |
| journalism and social science | 0.650 | 0.550 | 0.449 | 0.350 |
| finance analysis | 0.684 | 0.632 | 0.375 | 0.365 |

**Real embedders** (2026-10-11; Qwen3-Embedding 8B Q8_0, 4B and 0.6B f16 via
llama.cpp, last-token pooling, dim 1024, MemoryStore keyword stream; weighted
70% 8B / 20% 4B / 10% 0.6B):

| fixture | hit@5 off | hit@5 on | MRR off | MRR on | MRR change |
|---|---|---|---|---|---|
| all domains together | 0.898 | 0.874 | 0.725 | 0.644 | -0.081 |
| data analysis | 1.000 | 1.000 | 0.771 | 0.623 | -0.148 |
| software engineering | 0.911 | 0.895 | 0.673 | 0.655 | -0.018 |
| system administration | 0.937 | 0.931 | 0.726 | 0.528 | -0.198 |
| journalism and social science | 0.900 | 0.850 | 0.720 | 0.542 | -0.178 |
| finance analysis | 0.853 | 0.853 | 0.686 | 0.625 | -0.061 |

Per model, pooled MRR off to on: 8B 0.737 to 0.650, 4B 0.706 to 0.639,
0.6B 0.683 to 0.611. The pass rule is **not met** in any domain or pooled,
on every model. Confirmations here fall on 20% of entries regardless of
which query they answer, so a confirmed entry that is a close but wrong
match gains up to 1.25x over the right one at the floor. Real confirmations
probably correlate with usefulness, but nothing measured shows that yet, so
the setting stays off and the default does not change.

## A recency profile per kind (ADR 0072)

> **Removed (ADR 0103).** The per-kind profiles were re-measured on 2026-10-21 with the Qwen3 embedders, came out worse, and were taken out. The results below are kept as history; `tests/eval/confirmation.py` is now `tests/eval/ranking.py` and no longer measures it.

*(tools: `tests/eval/confirmation.py` variants, `make measure-ranking`;
`tests/eval/test_kind_profile.py` for the age-varied fixture)*
`HIVEMIND_RECENCY_BY_KIND` is unset by default. Set to
`insight=180,decision=365` (or `insight=90,decision=180`), decisions and
insights age by a longer half-life while facts keep 30 days, under the same
0.8 floor. The pass rule is ADR 0071's: no domain's or the pooled hit@5 or
MRR drops by more than 0.01, and the timeless slice improves. Everything
below is the hash embedder: **machinery only, not evidence**.

**Domain fixtures** (`tests/eval/domains.py`: 73 facts, 47 insights and 40
decisions in 160 entries, aged 0 to 400 days; the baseline is "confirmation
off", i.e. the shipped config). Hit@5 / MRR change against it:

| fixture | insight=180, decision=365 | insight=90, decision=180 |
|---|---|---|
| all domains together | -0.083 / -0.035 | -0.052 / -0.016 |
| data analysis | +0.053 / -0.022 | +0.000 / -0.026 |
| software engineering | -0.053 / -0.046 | +0.000 / +0.000 |
| system administration | +0.000 / -0.079 | +0.000 / -0.079 |
| journalism and social science | +0.000 / -0.044 | +0.000 / -0.002 |
| finance analysis | -0.053 / -0.011 | +0.000 / +0.000 |

Pooled baseline: hit@5 0.479, MRR 0.246. The pass rule is not met on the
hash embedder (the vector stream is close to noise at 4 dimensions, so these
numbers say little either way).

**Age-varied fixture** (`temporal.py`, shipped config vs the slow profile,
MRR; hit@5 is 1.000 throughout):

| ages of the newer currency members | slice | shipped | 180/365 |
|---|---|---|---|
| as written (5 to 12 days) | currency pairs | 0.778 | 0.833 |
| as written | timeless | 0.617 | 0.617 |
| as written | old-exact | 1.000 | 1.000 |
| 30 days | currency pairs | 0.583 | 0.611 |
| 30 days | timeless | 0.667 | 0.633 |
| 120 days | all slices | unchanged | unchanged |

The aged currency pairs gain a little while the newer member is inside the
profile's longer half-life, and nothing moves once everything is old
(120 days), because the floor is the same. The timeless slice does **not**
improve here: it is unchanged as written and falls 0.667 to 0.633 at 30
days, where a longer half-life keeps older entries closer to the top. The
pass rule's second clause is not met on this fixture. It is recorded as
measured; the real-embedder run decides.

**Real embedders** (2026-10-11; Qwen3-Embedding 8B Q8_0, 4B and 0.6B f16 via
llama.cpp, last-token pooling, dim 1024; weighted 70% 8B / 20% 4B / 10% 0.6B;
the baseline is the same run's "confirmation off"). hit@5 / MRR:

| fixture | baseline | insight=180, decision=365 | insight=90, decision=180 |
|---|---|---|---|
| all domains together | 0.894 / 0.728 | 0.878 / 0.687 | 0.895 / 0.722 |
| data analysis | 0.995 / 0.787 | 0.995 / 0.688 | 0.995 / 0.709 |
| software engineering | 0.911 / 0.664 | 0.947 / 0.653 | 0.947 / 0.689 |
| system administration | 0.937 / 0.758 | 0.937 / 0.633 | 0.937 / 0.677 |
| journalism and social science | 0.900 / 0.705 | 0.900 / 0.642 | 0.900 / 0.712 |
| finance analysis | 0.853 / 0.684 | 0.842 / 0.609 | 0.853 / 0.662 |

Pooled MRR per model, baseline to 90/180: 8B 0.727 to 0.719, 4B 0.744 to
0.744, 0.6B 0.700 to 0.696; with 180/365: 8B 0.686, 4B 0.708, 0.6B 0.649.

The pass rule (no drop beyond 0.01 in any domain or pooled) is **not met** by
either profile. 90/180 is nearly neutral pooled (-0.006 MRR) but costs data
analysis -0.078 and system administration -0.081 MRR. Caveat: this fixture's
queries do not favour old decisions or insights (ages are a deterministic
spread, unrelated to which answers are timeless), so it measures the cost of
the profile, not the benefit the setting is for (timeless knowledge not being
buried); a fixture with timeless old decisions would be needed to show the
benefit. The setting stays off.

## Similarity threshold

*(ADR 0062; tool: `tests/eval/threshold.py`, `make measure-threshold`)*
`HIVEMIND_VECTOR_MIN_SIMILARITY` defaults to 0.45 (ADR 0100); it was off
until the query instruction measurement below, and the sections here up to
that one record the reasoning and numbers from when it was off. The vector
stream keeps only entries whose cosine similarity to the query is above
it, so a search with nothing relevant can come back empty and shows up
in the empty-search counter (ADR 0056). The keyword stream is not
thresholded.

**Why it is measured per deployment.** Similarities depend on the model
and on its dimension: one model puts related text at 0.8 and unrelated
text at 0.4, another at 0.6 and 0.1. The hash embedder the other
experiments use cannot stand in for a real model here.

**Measured: 0.45 for the Qwen3-Embedding family at 1024 dimensions.**
*(2026-10-02; `tests/eval/threshold.py` on a scratch Postgres pool, so
the "search empty" figures use Postgres keyword matching. Models run
through llama.cpp with last-token pooling, truncated to 1024 dimensions
and re-normalised as a `dimensions` request does: 8B as the Q8_0 GGUF,
4B and 0.6B as f16. Embedded without a query
instruction (this run predates ADR 0097). Figures weight 8B 70%, 4B 20%, 0.6B 10%.)*

The multi-domain set (`tests/eval/domains.py`) has five kinds of agent
work: data analysis, software engineering, system administration,
journalism and social science, and finance analysis. Each has 32 entries
(a quarter with a body), 18 paraphrased queries its entries answer
and 6 in-domain queries none answers; 6 more queries ask one domain's
question of another's entry. Each domain is measured alone, as one
fleet's pool, where every other domain's queries are off-topic, and all
together (160 entries), as an org-wide reader sees the pool.

| pool | weakest answer (8B / 4B / 0.6B) | strongest off-topic (8B / 4B / 0.6B) |
|---|---|---|
| all domains together | 0.49 / 0.47 / 0.41 | 0.75 / 0.77 / 0.74 |
| data analysis | 0.59 / 0.56 / 0.52 | 0.79 / 0.77 / 0.74 |
| software engineering | 0.62 / 0.60 / 0.60 | 0.68 / 0.64 / 0.66 |
| system administration | 0.49 / 0.47 / 0.41 | 0.72 / 0.71 / 0.73 |
| journalism and social science | 0.66 / 0.62 / 0.51 | 0.66 / 0.70 / 0.68 |
| finance analysis | 0.52 / 0.57 / 0.45 | 0.71 / 0.69 / 0.72 |
| golden + age-varied | 0.65 / 0.66 / 0.73 | 0.62 / 0.62 / 0.68 |

**No value separates answers from off-topic queries** once queries are
paraphrased and the pool is varied. The weakest answers are vague
questions ("what should wake someone up at night" for the paging policy,
0.49 on 8B). The strongest off-topic queries are near misses that land
on a related entry ("minimum sample size rule for launching an A/B test"
on the A/B variance-reduction entry, 0.75), which an agent may well want
to see. The golden set alone suggested 0.60; on this set 0.60 drops 7% of
answers overall and 16% in the system administration pool.

| threshold | answers kept (all together) | answers kept (worst pool alone) | hit@5 (all together) | off-topic search empty (all together) | off-topic search empty (pools alone) |
|---|---|---|---|---|---|
| off | 100% | 100% | 0.914 | 0% | 0% |
| 0.45 | 100% | 99% (sysadmin) | 0.922 | 1% | 6–20% |
| 0.50 | 99% | 94% (sysadmin) | 0.923 | 1% | 16–36% |
| 0.55 | 98% | 94% (sysadmin) | 0.968 | 3% | 26–45% |
| 0.60 | 93% | 84% (sysadmin) | 0.966 | 3% | 31–53% |

In a pool alone, what the threshold empties is mostly the other domains'
queries, not the near misses inside the domain:

| pool alone | own unanswered queries, vector stream empty at 0.45 / 0.50 / 0.55 | other domains' queries, vector stream empty at 0.45 / 0.50 / 0.55 |
|---|---|---|
| data analysis | 0% / 7% / 33% | 11% / 36% / 61% |
| software engineering | 0% / 5% / 47% | 19% / 49% / 85% |
| system administration | 0% / 5% / 23% | 11% / 35% / 68% |
| journalism and social science | 0% / 17% / 28% | 18% / 48% / 77% |
| finance analysis | 3% / 8% / 57% | 24% / 50% / 73% |

Doubling the pools from 16 to 32 entries per domain lowered the weakest
answers in data analysis and finance (more entries compete for the same
query) and halved the share of off-topic searches that come back fully
empty, because a bigger pool more often shares a word with the query.
It also showed a side effect: in the 160-entry pool the threshold
*raises* hit@5, from 0.914 with it off to 0.968 at 0.55, because
unrelated vector matches no longer take fused ranks from the right
entry. The system administration pool goes the other way (0.995 to
0.947 at 0.50), where vague questions lose their only route in.

So 0.45 is the starting value: it loses almost no answers anywhere and
empties a tenth to a quarter of searches for another kind of work. 0.50
empties about twice as many of those and loses the vaguest answers (6%
in the system administration pool). In an org-wide pool almost nothing
comes back empty at any value that keeps the answers, because the
keyword stream finds a shared word and the near misses are genuinely
near. These are 156 synthetic queries over 184 entries: tune from there
with the counters below.

**Measured: expanded 16-domain corpus (0.25–0.30).**
*(2026-10-17; `tests/eval/corpus`, 16 domains × 128 entries = 2048, 646
answerable and 192 off-topic queries in the combined pool; Qwen3-Embedding
0.6B, 4B and 8B via vLLM at 1024 dimensions; `tests/eval/threshold.py`'s
`extended_scenarios()`, one fixture per process.)*

The five-domain set above is extended to sixteen domains (`tests/eval/corpus`):
the five original domains each grow from 32 to 128 entries (96 more, 22 more
answerable, 6 more unanswered), and eleven new domains are added (web search,
intelligence analysis, vulnerability research, blue team, red team, database
engineering, network operations, data engineering, platform engineering,
research literature, legal compliance), each 128 entries, 40 answerable,
12 unanswered. Every domain keeps the same shape: a quarter with a body, a
~70/20/10 fact/insight/decision mix, and off-topic queries answered in another
domain.

The combined pool (2048 entries, 646 answerable, 192 off-topic):

| model | weakest answer | strongest off-topic | keep all 646 answers at ≤ | off-topic starts emptying at ≥ | suggested |
|---|---|---|---|---|---|
| 0.6B | 0.343 | 0.890 | 0.30 | 0.55 | 0.25 |
| 4B | 0.354 | 0.897 | 0.35 | 0.50 | 0.30 |
| 8B | 0.393 | 0.902 | 0.35 | 0.55 | 0.30 |

The per-model combined sweep (answers kept in the vector stream / off-topic
vector stream empty):

| threshold | 0.6B | 4B | 8B |
|---|---|---|---|
| 0.30 | 646/646, 0/192 | 646/646, 0/192 | 646/646, 0/192 |
| 0.35 | 645/646, 0/192 | 646/646, 0/192 | 646/646, 0/192 |
| 0.45 | 639/646, 0/192 | 639/646, 0/192 | 640/646, 0/192 |
| 0.55 | 609/646, 6/192 | 604/646, 9/192 | 618/646, 4/192 |
| 0.60 | 572/646, 17/192 | 563/646, 17/192 | 573/646, 10/192 |
| 0.65 | 490/646, 51/192 | 489/646, 61/192 | 511/646, 34/192 |

The five-domain result (0.45) does not carry over: with sixteen domains the
weakest answer drops to 0.34–0.39 (more domains compete for the same
queries, and the new domains include vaguer question styles), so 0.45 already
loses 7 answers (1%) and 0.50 loses 14. The suggested value — the highest
0.05 step at least 0.05 below the weakest answer — is 0.25 on 0.6B and 0.30
on 4B and 8B. The trade-off gap is wider than on five domains: keeping
every answer needs ≤0.30–0.35, but off-topic queries only start to empty
the vector stream at ≥0.50. Between 0.35 and 0.50 the threshold loses
answers without emptying anything. The per-domain suggested values span
0.30 to 0.70 (blue team and database engineering are the highest, where
their entries are tightly clustered), which is the same reason the
five-domain study reported a per-pool range: one value is a starting
point, not a fit.

Caveat on the expanded corpus: the generator that produced the eleven
new domains and the five extensions gives a quarter of its entries a short
body, and those bodies are formulaic ("The N figure is the one the desk
verifies against the raw report before it is written to the pool"), so a
hundred of the 472 bodies repeat verbatim. That raises entry-to-entry
similarity within the pool, which is why the off-topic similarities sit high
(0.89–0.90) and the suggested value is conservative. The version-pair
entries carry no body, so the tau measurement is unaffected; and because the
same corpus is used for all three models, the relative comparison across
models is unaffected. The absolute thresholds should be re-checked on a
corpus with diverse bodies before a deployment sets the value from this
alone.

The summaries have the same problem, and more of it (checked 2026-10-18).
In nine of the eleven new domains (intelligence analysis, blue team, red
team, database engineering, network operations, data engineering, platform
engineering, research literature, legal compliance) 113 to 125 of each
domain's 128 summaries follow one template ("The compliance slo is the 10
with the 2 that are the critical and the 8 that are the standard", "The
vision is the 1 with the 2 that are the 2 and the 1 that is the 1"), about
1,100 of the 2,048, and many of their queries are keyword fragments ("siem
licence 5000"). 257 of the 768 version-pair entries use it too, so the tau
measurement is not fully clear of it either. On Qwen3-Embedding-8B the
threshold conclusions hold on the seven untemplated domains alone (weakest
answer 0.396, off-topic first empties at 0.55), but entry-to-entry
similarity is inflated: each entry's nearest other entry is at 0.85 or more
for 39% of the full pool, 7% of the untemplated domains and 1% of the five
original ones. Read the entry-to-entry figures (tau, the `near_duplicate`
band) from the untemplated domains.

Fixed on 2026-10-21: the templated domains were rewritten, and the
re-measured figures are in the last section, "The rewritten corpus".

**De-vagued queries make a high threshold viable.**
*(2026-10-17; `tests/eval/corpus/specific_queries.py`,
`tests/eval/threshold.py`'s `extended_scenarios_specific()`,
`EVAL_QUERY_STYLE=specific`; same entries and off-topic queries, only the
answerable query text differs.)*

The weakest answers above come from paraphrased queries ("how do we define a
churned customer"). An agent that gets nothing for a vague query typically
retries with a more specific one that names the system and the setting
("what inactivity window does the churn model use as the churn label"). The
question is whether that refined query is good enough for a high cut-off: if
so, a deployment can keep `HIVEMIND_VECTOR_MIN_SIMILARITY` high and rely on
the vague-then-specific loop, instead of lowering it to rescue vague queries.

Replacing the 646 answerable queries with their de-vagued variants (the
off-topic queries and the entries are unchanged) raises the weakest answer on
every model:

| model | weakest answer (vague → specific) | suggested (vague → specific) |
|---|---|---|
| 0.6B | 0.343 → 0.503 | 0.25 → 0.45 |
| 4B | 0.354 → 0.424 | 0.30 → 0.35 |
| 8B | 0.393 → 0.413 | 0.30 → 0.35 |

The lift is largest on 0.6B, where the weak model needs the extra lexical
overlap a specific query provides; on 4B and 8B the weak answers were already
higher, so the lift is smaller. In the combined pool, the specific queries
keep every answer at 0.45 on 0.6B and nearly all at 0.60 on every model:

| threshold | 0.6B vague → specific | 4B vague → specific | 8B vague → specific |
|---|---|---|---|
| 0.45 | 639 → 646 / 646 | 639 → 645 / 646 | 640 → 644 / 646 |
| 0.55 | 609 → 643 / 646 | 604 → 639 / 646 | 618 → 636 / 646 |
| 0.60 | 572 → 630 / 646 | 563 → 618 / 646 | 573 → 615 / 646 |
| 0.65 | 490 → 602 / 646 | 489 → 589 / 646 | 511 → 588 / 646 |

So the answer to the threshold question is yes: once queries are refined,
a high, selective cut-off (0.45–0.60) keeps the answers while emptying the
off-topic vector stream, and the five-domain value (0.45) becomes viable
again on 0.6B. The threshold is a property of the *query*, not just the
model — a deployment that gets specific queries can run a higher value than
one that gets vague ones.

**What it buys on 8B, through search (2026-10-18).** Re-run on
Qwen3-Embedding-8B (Q8_0 GGUF via llama.cpp, last-token pooling, 1024 dims;
it reproduces the vLLM figures above to within 0.004) with hit@5 through
`SearchService`, combined pool:

| threshold | vague: kept / hit@5 / off-topic vector empty | specific: kept / hit@5 / off-topic vector empty |
|---|---|---|
| off | 646 / 0.935 / 0 | 646 / 0.961 / 0 |
| 0.45 | 640 / 0.935 / 0 | 644 / 0.961 / 0 |
| 0.50 | 633 / 0.935 / 0 | 642 / 0.961 / 0 |
| 0.55 | 620 / 0.938 / 4 | 637 / 0.961 / 4 |
| 0.60 | 575 / 0.943 / 10 | 616 / 0.963 / 10 |
| 0.65 | 511 / 0.943 / 34 | 588 / 0.964 / 34 |

No off-topic search is ever empty (the keyword stream always matches). Up to
0.50 the threshold changes nothing on 8B; 0.55 is the first value with any
effect, and from there each step trades a few thousandths of hit@5 for
answers the vector stream drops. "Emptying the off-topic stream" is too
strong for 8B: 0.60 empties 10 of 192, because the off-topic queries are
in-domain questions with no answer (median nearest entry 0.71, inside the
answer band). On 8B the threshold does not pay for itself on this corpus,
so the default stayed off and no value was recommended. That was before
the query instruction (ADR 0097) was the default; with it, 0.45 became the
default (ADR 0100, last section).

**How to measure.** Point the `HIVEMIND_EMBEDDING_*` settings at the
embedder the deployment uses, with the deployment's dimension, and run:

    make vllm   # or use the production endpoint
    make measure-threshold HIVEMIND_EMBEDDING_DIM=1024

The tool embeds each scenario's entries the way writes do (the
multi-domain set together and per domain, and the golden and age-varied
corpora), then runs its answerable and off-topic queries. Nothing touches
Postgres unless a scratch `PgStore` is passed to `measure`. For each
scenario and each threshold from 0 to 0.9 it reports:

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

## A recency hint on search (ADR 0073)

*(tools: `tests/eval/test_recency_hint.py` for the age-varied fixture,
`tests/eval/ranking.py` variants and `make measure-ranking` for the domain
fixtures)* A search may carry `recency`: `ignore_age` (recency factor 1.0) or
`prefer_recent` (floor 0.5 instead of 0.8, the half-life kept). Everything below is the hash embedder: **machinery only, not
evidence**.

**Age-varied fixture, MRR per slice** (10 queries: 6 non-temporal, 4 temporal;
hit@5 is 1.000 unless noted). The queries' `temporal` label is the oracle:
`prefer_recent` for temporal queries, `ignore_age` for the rest.

| hint applied | non-temporal | temporal | old-exact | currency pairs | timeless | all |
|---|---|---|---|---|---|---|
| default (shipped) | 0.681 | 0.833 | 1.000 | 0.778 | 0.617 | 0.742 |
| `ignore_age`, every query | 0.917 | 0.708 | 1.000 | 0.611 | 0.900 | 0.833 |
| `prefer_recent` (floor 0.5), every query | 0.569 | 0.708 | 0.500 | 0.778 | 0.583 | 0.625 |
| `prefer_recent`, no floor, every query | 0.208 | 0.625 | 0.000 | 0.833 | 0.250 | 0.375 (hit@5 0.500) |
| oracle (0.5 floor) | 0.917 | 0.708 | 0.750 | 0.778 | 0.900 | 0.833 |
| oracle, no floor | 0.917 | 0.625 | 0.500 | 0.833 | 0.900 | 0.800 (hit@5 0.900) |
| `ignore_age` on non-temporal queries only | 0.917 | 0.833 | 1.000 | 0.778 | 0.900 | 0.883 |
| `prefer_recent` (0.5) on temporal queries only | 0.681 | 0.708 | 0.750 | 0.778 | 0.617 | 0.692 |

**The study's scratch claim, timeless MRR 0.607 to 0.900 with floor 1.0:
reproduced for the second figure, not the first.** `ignore_age` (and the
default scoring at floor 1.0, which is the same arithmetic here) gives timeless
MRR 0.900. The shipped baseline measures 0.617 in this repository (as in the
ADR 0072 section above), not 0.607. The gain, +0.283, is real on this
fixture; what it costs is the currency pairs (0.778 to 0.611) and the temporal
slice, which is why it is a per-search hint for a question the agent knows to
be timeless, and not a default.

**`prefer_recent` is not a win on this fixture, with either floor.** With no
floor one currency pair gains a rank (0.778 to 0.833) and every exact match
that is not the newest drops out (non-temporal 0.208, hit@5 0.333): the
unbounded term spans more than the fused range. A floor of 0.5 (2x, inside the
fused range of 2.62x) improves no slice and loses the old-exact answer to a
temporal-worded query (1.000 to 0.500 applied to all, 0.750 under the oracle).
Floors of 0.7 and up give the default's numbers, 0.4 and below the no-floor
ones. 0.5 is shipped for its bounded cost, not for a measured gain: a wrong
hint should cost a bounded amount. The oracle's temporal slice gets worse
(0.833 to 0.708) because the one temporal-worded query whose answer is old
(T1) is mislabelled by its wording; the "time sense" is the agent's to judge,
and that example is the reason the server does not infer it.

**Domain fixtures** (160 entries aged 0 to 400 days, 96 answerable queries,
every query given the same hint because the domain fixtures do not label time
sense; the baseline is the shipped config). Hit@5 / MRR change:

| fixture | `ignore_age` | `prefer_recent` (0.5) |
|---|---|---|
| all domains together | +0.052 / +0.023 | -0.052 / -0.037 |
| data analysis | +0.000 / +0.041 | +0.000 / -0.026 |
| software engineering | -0.053 / +0.000 | +0.000 / -0.035 |
| system administration | +0.000 / +0.009 | +0.000 / -0.018 |
| journalism and social science | -0.100 / -0.092 | -0.100 / -0.035 |
| finance analysis | +0.000 / +0.000 | -0.053 / -0.030 |

Pooled baseline: hit@5 0.448, MRR 0.233. The pass rule of ADR 0071 (no drop
beyond 0.01) is a rule for a setting applied to everything; a hint is applied
by an agent per query, so the table above shows the cost of a wrong or
overused hint, and the benefit needs a time-labelled real fixture.

**Real embedders** (2026-10-11; Qwen3-Embedding 8B Q8_0, 4B and 0.6B f16 via
llama.cpp, last-token pooling, dim 1024; weighted 70% 8B / 20% 4B / 10% 0.6B;
hint applied to every query; the baseline is this run's default). hit@5 / MRR:

| fixture | default | `ignore_age` | `prefer_recent` |
|---|---|---|---|
| all domains together | 0.909 / 0.737 | 0.917 / 0.752 | 0.877 / 0.687 |
| data analysis | 1.000 / 0.816 | 1.000 / 0.901 | 1.000 / 0.684 |
| software engineering | 0.911 / 0.661 | 0.911 / 0.758 | 0.900 / 0.638 |
| system administration | 0.937 / 0.744 | 0.984 / 0.851 | 0.900 / 0.655 |
| journalism and social science | 0.890 / 0.710 | 0.900 / 0.719 | 0.890 / 0.670 |
| finance analysis | 0.853 / 0.705 | 0.853 / 0.729 | 0.858 / 0.668 |

Pooled MRR per model, default to `ignore_age` to `prefer_recent`: 8B 0.745 to
0.756 to 0.691, 4B 0.723 to 0.746 to 0.682, 0.6B 0.709 to 0.732 to 0.671.

On these mostly timeless queries `ignore_age` improves MRR in every domain
(+0.009 to +0.107) and pooled (+0.015), and hit@5 never drops. `prefer_recent`
lowers MRR everywhere, as expected for timeless questions, and remains
unproven for current-state questions: there is no real-embedder fixture with
labelled current-state queries yet. A hint acts only when an agent sends it, so
turning `HIVEMIND_RECENCY_HINT` on is low-risk; the measurement supports
enabling it for `ignore_age`. *(Superseded: ADR 0101 turned the hint on by
default, after the re-run below.)*

**Re-run with the query instruction on (2026-10-21; ADR 0101).** Same fixture
and harness (`make measure-confirmation`, now `make measure-ranking`; five domains, 160 entries aged 0 to
400 days, 96 answerable queries, the hint applied to every query), with the
query instruction on (ADR 0097) and each embedder reported alone: Qwen3-Embedding
8B Q8_0, 4B f16 and 0.6B f16 via llama.cpp, dim 1024. Pooled MRR, no hint, then
`ignore_age`, then `prefer_recent`:

| embedder | no hint | `ignore_age` | `prefer_recent` |
|---|---|---|---|
| 8B | 0.729 | 0.738 | 0.724 |
| 4B | 0.741 | 0.745 | 0.687 |
| 0.6B | 0.711 | 0.743 | 0.653 |

Pooled hit@5 never drops with `ignore_age`: 8B 0.906 to 0.906, 4B 0.896 to
0.896, 0.6B 0.906 to 0.927. Change in MRR with `ignore_age`, per domain:

| domain | 8B | 4B | 0.6B |
|---|---|---|---|
| data analysis | +0.121 | +0.132 | +0.066 |
| software engineering | +0.092 | +0.079 | +0.018 |
| system administration | +0.079 | +0.070 | +0.082 |
| journalism and social science | +0.027 | -0.029 | +0.027 |
| finance analysis | +0.088 | +0.053 | +0.026 |

`ignore_age` helps in 14 of 15 cells; the one loss is journalism on the 4B
model. `prefer_recent` lowers MRR in every domain on these mostly timeless
queries; it stays for current-state questions, for which there is still no
labelled real-embedder fixture. The hint is on by default, and the `hive_search`
description sends agents to `ignore_age` for a question whose answer does not
age and to `prefer_recent` only for a "what is current" question.

## Version grouping

ADR 0074 shows unlinked near-copies of one fact next to each other, newest
first, when `HIVEMIND_VERSION_GROUP_MIN_SIMILARITY` (tau) is set. Whether two
entries are versions or merely about the same system depends on the
embedding model, so tau is measured with the model a deployment runs:

    make vllm   # or use the production endpoint
    make measure-versions HIVEMIND_EMBEDDING_DIM=1024

For each of the five domains of the threshold study, the pool is the
domain's own 32 entries plus six pairs of an older and a newer unlinked
statement of one fact (a changed value: "pool size is 20", later "raised to
90") and six same-system distractors (another setting of the same service).
The queries for the newer entry use the older statement's words, so the older
one often has the higher lexical overlap, the case grouping is for. For
grouping off and each tau from 0.70 to 0.95 it reports, per domain and
pooled:

| column | meaning |
|---|---|
| false grouping | distractors grouped with a version member or another distractor, in a search for their pair's fact |
| true grouping | version pairs grouped in a search for the fact |
| newer-member MRR | reciprocal rank of the newer entry for the query about the fact |
| old-exact MRR | the same for the older entry and a query quoting it (it can only fall: the newer member moves ahead) |
| hit@5 | over all the domain's queries: the two above and its answerable queries |
| groups with other entries | groups touching the domain's own entries (information) |

Adopt a tau only where pooled false grouping is at most 5%, no domain is
above 10%, the sysadmin domain passes (false grouping at most 5%, no loss
against off, old-exact within 0.02), newer-member MRR is up, old-exact MRR is
at most 0.02 below baseline (it can only fall, so "not below" could never pass) and
pooled hit@5 is not lower. The tool prints the verdict for every tau and the
lowest passing one.

`tests/eval/test_version_measurement.py` runs the same code on the 4-dimension
hash embedder; that checks the machinery, and its numbers mean nothing.

Measured 2026-10-11 with Qwen3-Embedding 8B, 4B and 0.6B (llama.cpp GGUF,
vectors cut to 1024 dimensions), weighted 70/20/10, pooled over the five
domains:

| tau | false grouping | true grouping | newer-member MRR | old-exact MRR | hit@5 |
|---|---|---|---|---|---|
| off | 0% | 0% | 0.201 | 0.260 | 0.762 |
| 0.70 | 66% | 74% | 0.255 | 0.186 | 0.743 |
| 0.75 | 48% | 70% | 0.239 | 0.201 | 0.750 |
| 0.80 | 31% | 54% | 0.230 | 0.225 | 0.743 |
| 0.85 | 6% | 37% | 0.225 | 0.232 | 0.759 |
| 0.90 | 1% | 12% | 0.212 | 0.248 | 0.760 |
| 0.95 | 0% | 1% | 0.202 | 0.259 | 0.762 |

No tau passes on any of the three models, so the suggestion is none: keep
grouping off. The two sides never meet. Where false grouping is low enough
(0.90 and up), grouping finds few true pairs and the newer-member gain is
about 0.01; sysadmin, whose distractors are other settings of the same
service, still loses against off at 0.90 on every model, and data analysis
groups 17% of its distractors at 0.90 on 0.6B and 4B. Below 0.90, a third
or more of the distractors are grouped. Same-system facts sit as close
together in these embedders as two versions of one fact, so cosine alone
cannot tell them apart.

**Measured: expanded 16-domain corpus — still no passing tau.**
*(2026-10-17; `tests/eval/corpus`, 16 domains × 16 version pairs = 256
pairs, 2048 background entries; Qwen3-Embedding 0.6B, 4B, 8B via vLLM at
1024 dimensions; `tests/eval/versions.py`'s `measure_all()` over four
fleet constellations)*

The five-domain fixture is extended to sixteen domains (`tests/eval/corpus`):
each domain now carries 16 version pairs (6 original + 10 from its
extension) and 128 background entries, for 256 pairs and 2048 background
entries in total. The harness also sweeps four fleet constellations —
org-only (legacy), an isolated single fleet (the adoption rule's
constellation), two mixed fleets with overlapping systems, and org
alongside a fleet — so the fleet rule can be read against the
similarities. The adoption rule is applied to the isolated-fleet
constellation only; the others are sensitivity readings.

Pooled over the sixteen domains (256 pairs), isolated fleet:

| model | tau | false grouping | true grouping | newer-member MRR | old-exact MRR | hit@5 |
|---|---|---|---|---|---|---|
| 0.6B | off | 0% | 0% | 0.196 | 0.266 | 0.790 |
| 0.6B | 0.90 | 14% | 71% | 0.243 | 0.261 | 0.800 |
| 0.6B | 0.95 | 0% | 52% | 0.214 | 0.248 | 0.776 |
| 4B | off | 0% | 0% | 0.202 | 0.273 | 0.786 |
| 4B | 0.90 | 5% | 61% | 0.226 | 0.251 | 0.778 |
| 4B | 0.95 | 1% | 45% | 0.214 | 0.244 | 0.788 |
| 8B | off | 0% | 0% | 0.204 | 0.292 | 0.773 |
| 8B | 0.90 | 6% | 52% | 0.227 | 0.274 | 0.766 |
| 8B | 0.95 | 0% | 32% | 0.216 | 0.272 | 0.766 |

No tau passes the adoption rule on any model, on the larger corpus. The
4B model at tau=0.90 is the closest: pooled false grouping is exactly
5% (meets "at most 5%") and true grouping is 61%, but it fails on
three counts — old-exact MRR drops 0.022 (0.273 to 0.251, over the
0.02 allowance), hit@5 drops 0.008 (0.786 to 0.778), and three
domains exceed 10% false grouping (data analysis 12%, web search
19%, database engineering 25%). 0.6B and 8B are further off (14%
and 6% pooled false grouping at 0.90). Raising tau to 0.95 cuts
false grouping to 0–1% but true grouping falls to 32–52%, so the
newer-member gain is small and old-exact stays down.

The conclusion matches the five-domain study and is now established
on 8× the pairs: same-system facts sit as close together in these
embedders as two versions of one fact, so a single cosine cut-off
cannot separate them. The fleet constellations change what is
eligible to group (an isolated fleet sees only its own entries), not
the similarities, so they do not rescue a tau: the isolated-fleet
constellation (the one the adoption rule uses) is the most
favourable and it still fails. **Keep grouping off.**

**De-vagued pair queries do not help the tau.**
*(2026-10-17; `tests/eval/corpus/specific_queries.py`,
`tests/eval/versions.py`'s `query_style="specific"`;
same entries, only each pair's `query` is de-vagued.)*

Where the threshold is a property of the *query*, the tau is a property of
the *entries*: grouping is decided by the entry-to-entry similarity between
the anchor and each candidate, and the query only decides which candidates
reach the top of the list. Replacing each pair's query with its de-vagued
variant (naming the exact setting) does not move the pooled false-grouping
rate on any model — 0.6B 14% → 14%, 4B 5% → 5%, 8B 6% → 7% at tau=0.90 —
and does not lift true grouping. The distractors are near-copies of the
version members ("The postgres checkpoint is the 5 minute" beside "has the 1
time"), so a specific query ranks them into the candidate list but cannot
stop them grouping: their cosine to the anchor is what groups them. A
passing tau needs the *distractors* to be less similar to the version members
(genuinely different settings, not near-copies), not the *queries* to be
more specific.

This bears on how representative the corpus is. Hivemind already steers
writers away from near-copies — the skill says to recall before writing and
to "supersede, don't duplicate", and every write answers with `related`, its
up-to-3 nearest entries with a cosine `similarity`, so a writer can see a
near-duplicate before committing (ADR 0052, 0077). That convention is
advisory, not enforced: there is no similarity floor that refuses a
near-duplicate (ADR 0052 rejected one, because an untuned floor blocks
legitimate writes). The corpus's near-copied distractors are therefore a
worst case — a pool where no writer superseded — and the real false-grouping
rate should be lower, because the `related` reply and the supersede convention
keep some near-copies out of the pool. A `near_duplicate` advisory warning
(SPEC §4.1, ADR 0087) now makes that signal explicit without refusing
anything: it fires when a write's nearest `related` entry (ADR 0052), other
than one the draft links with `see_also`, is at similarity ≥ 0.90, and asks
the writer to rate it, supersede it or link it, whichever fits. On
Qwen3-Embedding-8B over the seven untemplated domains (112 pairs), 0.90
catches 71% of version pairs and 1% of same-system distractors; 0.85 would
catch 84% and 7% (a distinct fact such as "the Airflow default pool has 128
slots" beside "the backfill pool has 2048 slots" sits at 0.883), and 0.95
catches 54% and 0%.

## A narrower importance factor (ADR 0088)

*(tool: `tests/eval/importance.py`, `make measure-importance`; set
`EVAL_IMPORTANCE_JSON=path` to also write the rows as JSON)*
`HIVEMIND_NARROW_IMPORTANCE` is on by default. The harness seeds each of the
sixteen domains alone, and all of them pooled, under three importance
assignments (`equal`: all 3; `random`: seeded 1 to 5 independent of
relevance; `aligned`: answers 4 or 5, everything else random) and scores each
with the narrow factor off and on: hit@1, hit@5 and MRR, and the change.
`equal` must show no change. Run it with the embedders a deployment uses.

Run on 2026-10-18 with Qwen3-Embedding 8B, 4B and 0.6B at 1024 dimensions
(llama.cpp, last-token pooling, truncated and renormalised), 646 queries
pooled. Cells are hit@1 / hit@5 / MRR, off then on.

| model | importance | narrow off | narrow on | change (hit@1 / hit@5 / MRR) |
|---|---|---|---|---|
| 8B | equal | 0.748 / 0.941 / 0.829 | 0.748 / 0.941 / 0.829 | +0.000 / +0.000 / +0.000 |
| 8B | random | 0.458 / 0.847 / 0.602 | 0.542 / 0.892 / 0.676 | +0.084 / +0.045 / +0.075 |
| 8B | aligned | 0.659 / 0.909 / 0.763 | 0.751 / 0.940 / 0.828 | +0.091 / +0.031 / +0.065 |
| 4B | equal | 0.783 / 0.958 / 0.855 | 0.783 / 0.958 / 0.855 | +0.000 / +0.000 / +0.000 |
| 4B | random | 0.463 / 0.864 / 0.613 | 0.553 / 0.907 / 0.691 | +0.090 / +0.043 / +0.079 |
| 4B | aligned | 0.681 / 0.932 / 0.787 | 0.763 / 0.960 / 0.843 | +0.082 / +0.028 / +0.056 |
| 0.6B | equal | 0.810 / 0.954 / 0.870 | 0.810 / 0.954 / 0.870 | +0.000 / +0.000 / +0.000 |
| 0.6B | random | 0.471 / 0.859 / 0.616 | 0.559 / 0.913 / 0.697 | +0.088 / +0.054 / +0.080 |
| 0.6B | aligned | 0.676 / 0.935 / 0.784 | 0.785 / 0.955 / 0.855 | +0.108 / +0.020 / +0.071 |

Weighted 70/20/10 (8B/4B/0.6B), pooled MRR changes by +0.076 under
`random` and +0.064 under `aligned`; `equal` is unchanged. MRR change per
domain:

| domain | 8B random | 8B aligned | 4B random | 4B aligned | 0.6B random | 0.6B aligned |
|---|---|---|---|---|---|---|
| data analysis | +0.086 | +0.025 | +0.128 | +0.053 | +0.133 | +0.096 |
| software engineering | +0.074 | +0.029 | +0.108 | +0.034 | +0.100 | +0.071 |
| system administration | +0.120 | +0.056 | +0.076 | +0.037 | +0.098 | +0.038 |
| journalism and social science | +0.060 | +0.052 | +0.040 | +0.021 | +0.026 | +0.053 |
| finance analysis | +0.055 | +0.015 | +0.037 | +0.030 | +0.052 | +0.007 |
| web search | +0.046 | +0.008 | +0.055 | +0.055 | +0.028 | +0.054 |
| intelligence analysis | +0.083 | +0.102 | +0.059 | +0.048 | +0.059 | +0.079 |
| vulnerability research | +0.020 | +0.035 | +0.004 | +0.049 | +0.035 | +0.044 |
| blue team | +0.065 | +0.147 | +0.083 | +0.191 | +0.135 | +0.172 |
| red team | +0.127 | +0.052 | +0.140 | +0.063 | +0.129 | +0.021 |
| database engineering | +0.104 | +0.119 | +0.166 | +0.093 | +0.125 | +0.106 |
| network operations | +0.139 | +0.125 | +0.175 | +0.175 | +0.180 | +0.156 |
| data engineering | +0.155 | +0.096 | +0.195 | +0.161 | +0.162 | +0.073 |
| platform engineering | +0.172 | +0.035 | +0.172 | +0.165 | +0.172 | +0.083 |
| research literature | +0.061 | +0.020 | +0.109 | +0.145 | +0.197 | +0.110 |
| legal compliance | +0.019 | +0.062 | +0.047 | +0.091 | +0.060 | +0.077 |

MRR rises in every domain on every model. The only drops are single queries
(0.025 in a 40-query domain) in hit@1 or hit@5 in six of the 96 domain rows.
`aligned` was meant as the wide factor's best case and still favours the
narrow one: every answer gets 4 or 5, including answers that are distractors
for other queries, and a 5 on a near-miss beats a 4 on the right answer by
1.25x under the wide factor. On these results the setting ships on (ADR
0088).

## Query instruction (ADR 0097)

*(tool: `tests/eval/query_instruction.py`, `make measure-query-instruction`;
thresholds from `tests/eval/threshold.py`)*
`HIVEMIND_QUERY_INSTRUCTION` is on by default. The search query sent to the
vector stream is embedded as `Instruct: {instruction}\nQuery:{query}`; entries
never carry it. The harness scores every answerable query of the sixteen
domain fixtures, alone and pooled, with no instruction and with the generic
one (`Given an agent's task or question, retrieve shared memory entries that
answer it`). These figures were measured on the templated corpus;
the re-measurement on the rewritten one is in the last section, "The
rewritten corpus".

**Setup.** Qwen3-Embedding through llama.cpp GGUFs with last-token pooling at
1024 dimensions: 8B as Q8_0, 4B and 0.6B as f16. The sixteen domain fixtures,
646 answerable queries pooled. Weighted figures use 70% 8B, 20% 4B, 10% 0.6B.

**Ranking.** Pooled hit@1 / hit@5 / MRR, no instruction then the generic one.

| model | none | generic | change (MRR) |
|---|---|---|---|
| 8B | 0.746 / 0.938 / 0.826 | 0.810 / 0.966 / 0.878 | +0.051 |
| 4B | 0.776 / 0.957 / 0.853 | 0.828 / 0.971 / 0.888 | +0.035 |
| 0.6B | 0.803 / 0.954 / 0.865 | 0.833 / 0.960 / 0.887 | +0.023 |

Weighted pooled MRR goes from 0.835 to 0.881 (+0.046). Per domain the mean
MRR change is +0.027 on 8B (6 of 16 domains slightly worse, the worst
-0.037; the best, legal compliance, +0.187), +0.013 on 4B (7 worse, worst
-0.037) and +0.002 on 0.6B (6 worse, worst -0.046); weighted, +0.022. So the
gain is real in the pool and uneven by domain. A second wording, `Given a
question or task description, retrieve a stored note that answers it`, gave
the same result on 0.6B and 4B.

The 8B run, per domain:

| fixture | instruction | queries | hit@1 / hit@5 / MRR | change vs none (hit@1 / hit@5 / MRR) |
|---|---|---|---|---|
| all 16 domains together | none | 646 | 0.746 / 0.938 / 0.826 | baseline |
| all 16 domains together | generic | 646 | 0.810 / 0.966 / 0.878 | +0.063 / +0.028 / +0.051 |
| data analysis | none | 41 | 0.732 / 0.829 / 0.774 | baseline |
| data analysis | generic | 41 | 0.659 / 0.829 / 0.738 | -0.073 / +0.000 / -0.037 |
| software engineering | none | 41 | 0.756 / 0.951 / 0.824 | baseline |
| software engineering | generic | 41 | 0.780 / 0.951 / 0.833 | +0.024 / +0.000 / +0.009 |
| system administration | none | 41 | 0.780 / 1.000 / 0.873 | baseline |
| system administration | generic | 41 | 0.854 / 0.976 / 0.903 | +0.073 / -0.024 / +0.030 |
| journalism and social science | none | 42 | 0.714 / 0.857 / 0.778 | baseline |
| journalism and social science | generic | 42 | 0.738 / 0.857 / 0.788 | +0.024 / +0.000 / +0.010 |
| finance analysis | none | 41 | 0.707 / 0.951 / 0.809 | baseline |
| finance analysis | generic | 41 | 0.659 / 0.951 / 0.774 | -0.049 / +0.000 / -0.035 |
| web search | none | 40 | 0.725 / 0.975 / 0.838 | baseline |
| web search | generic | 40 | 0.775 / 0.975 / 0.871 | +0.050 / +0.000 / +0.033 |
| intelligence analysis | none | 40 | 0.875 / 0.975 / 0.925 | baseline |
| intelligence analysis | generic | 40 | 0.825 / 1.000 / 0.906 | -0.050 / +0.025 / -0.019 |
| vulnerability research | none | 40 | 0.925 / 1.000 / 0.963 | baseline |
| vulnerability research | generic | 40 | 0.925 / 1.000 / 0.963 | +0.000 / +0.000 / +0.000 |
| blue team | none | 40 | 0.700 / 1.000 / 0.842 | baseline |
| blue team | generic | 40 | 0.675 / 1.000 / 0.823 | -0.025 / +0.000 / -0.019 |
| red team | none | 40 | 0.600 / 0.875 / 0.726 | baseline |
| red team | generic | 40 | 0.625 / 0.975 / 0.792 | +0.025 / +0.100 / +0.066 |
| database engineering | none | 40 | 0.600 / 1.000 / 0.796 | baseline |
| database engineering | generic | 40 | 0.700 / 1.000 / 0.842 | +0.100 / +0.000 / +0.046 |
| network operations | none | 40 | 0.850 / 1.000 / 0.925 | baseline |
| network operations | generic | 40 | 0.825 / 1.000 / 0.912 | -0.025 / +0.000 / -0.013 |
| data engineering | none | 40 | 0.675 / 0.875 / 0.767 | baseline |
| data engineering | generic | 40 | 0.750 / 0.975 / 0.854 | +0.075 / +0.100 / +0.087 |
| platform engineering | none | 40 | 0.800 / 1.000 / 0.882 | baseline |
| platform engineering | generic | 40 | 0.750 / 1.000 / 0.865 | -0.050 / +0.000 / -0.018 |
| research literature | none | 40 | 0.650 / 0.850 / 0.733 | baseline |
| research literature | generic | 40 | 0.725 / 1.000 / 0.844 | +0.075 / +0.150 / +0.110 |
| legal compliance | none | 40 | 0.650 / 0.925 / 0.751 | baseline |
| legal compliance | generic | 40 | 0.900 / 0.975 / 0.938 | +0.250 / +0.050 / +0.187 |

**Answer similarity.** Cosine similarity of the best answer to the query,
pooled, mean / 10th percentile.

| model | none | generic |
|---|---|---|
| 8B | 0.741 / 0.594 | 0.712 / 0.569 |
| 4B | 0.730 / 0.582 | 0.713 / 0.571 |
| 0.6B | 0.727 / 0.590 | 0.723 / 0.582 |

The instruction lowers answer similarity a little (p10 by 0.008 to 0.025).

**Threshold.** Cells are kept / emptied: the share of answerable queries
whose best answer clears t, and the share of off-topic queries whose nearest
entry stays below t.

| model | pool | instruction | t=0.35 | t=0.40 | t=0.45 | t=0.50 | t=0.55 | t=0.60 |
|---|---|---|---|---|---|---|---|---|
| 8B | all 16 domains together | none | 1.00/0.00 | 1.00/0.00 | 0.99/0.00 | 0.98/0.00 | 0.96/0.02 | 0.89/0.05 |
| 8B | all 16 domains together | generic | 1.00/0.00 | 0.99/0.00 | 0.98/0.03 | 0.97/0.05 | 0.92/0.07 | 0.85/0.17 |
| 8B | mean of 16 single-domain pools | none | 1.00/0.00 | 1.00/0.00 | 0.99/0.04 | 0.98/0.18 | 0.96/0.48 | 0.89/0.76 |
| 8B | mean of 16 single-domain pools | generic | 1.00/0.12 | 0.99/0.31 | 0.98/0.55 | 0.97/0.75 | 0.92/0.88 | 0.85/0.94 |
| 4B | all 16 domains together | none | 1.00/0.00 | 1.00/0.00 | 0.99/0.00 | 0.97/0.01 | 0.93/0.05 | 0.87/0.09 |
| 4B | all 16 domains together | generic | 1.00/0.00 | 0.99/0.01 | 0.98/0.01 | 0.97/0.05 | 0.93/0.08 | 0.85/0.21 |
| 4B | mean of 16 single-domain pools | none | 1.00/0.00 | 1.00/0.03 | 0.99/0.15 | 0.97/0.41 | 0.94/0.71 | 0.87/0.88 |
| 4B | mean of 16 single-domain pools | generic | 1.00/0.10 | 0.99/0.30 | 0.98/0.55 | 0.97/0.76 | 0.93/0.88 | 0.85/0.95 |
| 0.6B | all 16 domains together | none | 1.00/0.00 | 1.00/0.00 | 0.99/0.00 | 0.98/0.00 | 0.94/0.03 | 0.88/0.09 |
| 0.6B | all 16 domains together | generic | 0.99/0.00 | 0.99/0.00 | 0.98/0.03 | 0.97/0.05 | 0.93/0.12 | 0.86/0.29 |
| 0.6B | mean of 16 single-domain pools | none | 1.00/0.00 | 1.00/0.01 | 0.99/0.09 | 0.98/0.30 | 0.94/0.62 | 0.88/0.84 |
| 0.6B | mean of 16 single-domain pools | generic | 0.99/0.08 | 0.99/0.27 | 0.98/0.57 | 0.97/0.79 | 0.93/0.90 | 0.86/0.95 |

The instruction lowers answer similarity slightly but off-topic similarity
much more, so a threshold separates the two better. At 0.45 in single-domain
pools, the share of off-topic searches left empty rises from 4% to 55% on 8B
(15% to 55% on 4B, 9% to 57% on 0.6B) while 98% of answers are kept (99%
before). 0.45 is the recommendation, and with the instruction on by
default it became the default too (ADR 0100); 0.50 keeps 97% of answers and
0.55 keeps 92%. Set `HIVEMIND_VECTOR_MIN_SIMILARITY` empty to turn it off.

**Search-aware strategies (4B only).** Two ways of using the query's shape or
the kind filter were tried against the single generic instruction.

| strategy | pooled MRR | per-domain mean MRR |
|---|---|---|
| generic instruction | 0.892 | 0.869 |
| instruction chosen by query shape (question, error, topic) | 0.893 | 0.860 |
| generic instruction, kind filter set | 0.909 | |
| kind-specific instruction, kind filter set | 0.908 | |

Neither beats the generic instruction, so there is one instruction for every
route. The corpus is mostly topic-style queries (558 topic, 61 question, 27
error), and the recency and explore routes cannot be measured with it.

**Version ranking (`make measure-versions`, 8B and 0.6B).** Whether two
entries group as versions compares entries with each other, so the
instruction does not change it. What it can change is where each version
ranks. Means over the sixteen domains, with grouping off (the default):

| model | instruction | newer-member MRR | old-exact MRR | hit@5 |
|---|---|---|---|---|
| 8B | none | 0.191 to 0.202 | 0.283 to 0.300 | 0.767 to 0.786 |
| 8B | generic | 0.183 to 0.188 | 0.242 to 0.254 | 0.765 to 0.780 |
| 0.6B | none | 0.205 to 0.212 | 0.265 to 0.278 | 0.774 to 0.797 |
| 0.6B | generic | 0.181 to 0.190 | 0.234 to 0.239 | 0.759 to 0.772 |

Each range spans the four fleet constellations of the harness. The newer
member is asked for in the older statement's words, and the old-exact query
quotes the older entry. These statement-shaped queries look like documents,
so the instruction moves them away from the entry they quote: old-exact MRR
drops by about 0.04 on 8B, newer-member MRR by about 0.01, and hit@5 barely
moves. This is the one place the instruction costs something. It is far
smaller than the gain on ordinary searches, so the default stays on.

**Importance (`make measure-importance`, 0.6B).** Narrowed importance (ADR
0088) gives the same lift with the instruction as without it: pooled MRR
with aligned importance rises 0.782 to 0.851 without the instruction and
0.782 to 0.854 with it.

## The rewritten corpus (2026-10-21)

The templated domains named in the caveat under "Similarity threshold"
(and vulnerability research, which used a second template: "The X is in
the Y where Z is done with the W") were rewritten with varied, specific
entries, bodies, version pairs and queries; web search kept its summaries.
Sizes and labels are unchanged. `tests/eval/corpus/quality.py` now fails a
templated domain without an embedder, and the normal test suite runs it.
The per-domain and pooled figures in the earlier sections of this file
predate the rewrite.

**Setup.** Qwen3-Embedding 8B, 4B and 0.6B as Q8_0 GGUFs through llama.cpp
(last-token pooling, first 1024 dimensions, L2-normalised), on CPU. The
sixteen domain fixtures alone and pooled (646 answerable queries), entries
aged as in the other harnesses, `SearchService` at k = 5 with the 2.11.0
defaults: query instruction on, `HIVEMIND_VECTOR_MIN_SIMILARITY` 0.45, the
`ignore_age` recency hint agents send. Each other row turns one of those
off. Weighted figures use 70% 8B, 20% 4B, 10% 0.6B.

**Ranking (MRR).** Pooled, then the mean of the sixteen single-domain pools.

| configuration | 8B | 4B | 0.6B | weighted |
|---|---|---|---|---|
| 2.11.0 defaults, pooled | 0.856 | 0.843 | 0.846 | 0.853 |
| no query instruction | 0.852 | 0.839 | 0.832 | 0.847 |
| no cut-off | 0.855 | 0.842 | 0.846 | 0.852 |
| `default` recency hint | 0.840 | 0.829 | 0.832 | 0.837 |
| all three off (2.10.0) | 0.839 | 0.825 | 0.815 | 0.834 |
| 2.11.0 defaults, domain mean | 0.887 | 0.887 | 0.895 | 0.888 |
| no query instruction | 0.882 | 0.885 | 0.884 | 0.883 |
| no cut-off | 0.881 | 0.883 | 0.888 | 0.882 |
| `default` recency hint | 0.844 | 0.849 | 0.858 | 0.846 |
| all three off (2.10.0) | 0.838 | 0.844 | 0.838 | 0.839 |

Every 2.11.0 default still helps on every model. The `ignore_age` hint is
the largest single gain (0.042 weighted, domain mean). The query
instruction gains far less than the ADR 0097 measurement found (pooled 8B
+0.004 here against +0.051 there): most of that gain came from the
templated domains (legal compliance +0.187, research literature +0.110,
data engineering +0.087 on 8B), whose keyword-fragment queries the
instruction helped. Weighted per domain, the instruction is worse in four
of sixteen domains, by at most 0.016 (blue team); none of them is a
regression worth a per-domain setting.

**Cut-off sweep, single-domain pools.** Cells are answers kept in the
vector stream / off-topic queries with an empty vector stream (every query
the pool cannot answer, mostly other domains' questions) / in-domain
unanswered queries with an empty vector stream / mean MRR.

| t | 8B | 4B | 0.6B |
|---|---|---|---|
| off | 100% / 0% / 0% / 0.881 | 100% / 0% / 0% / 0.883 | 100% / 0% / 0% / 0.888 |
| 0.40 | 99% / 20% / 1% / 0.883 | 99% / 18% / 1% / 0.884 | 99% / 18% / 1% / 0.889 |
| 0.45 | 99% / 41% / 3% / 0.887 | 98% / 38% / 3% / 0.887 | 99% / 43% / 5% / 0.895 |
| 0.50 | 98% / 65% / 7% / 0.892 | 98% / 62% / 6% / 0.896 | 97% / 71% / 9% / 0.903 |
| 0.55 | 97% / 83% / 16% / 0.903 | 97% / 82% / 16% / 0.901 | 95% / 88% / 29% / 0.904 |
| 0.60 | 94% / 92% / 28% / 0.906 | 93% / 92% / 32% / 0.901 | 89% / 96% / 53% / 0.901 |

0.45 keeps 99% of answers on 8B and empties the vector stream for 41% of
off-topic searches (55% on the old corpus, where the templated domains
were easy to tell apart). In-domain questions with no answer stay close to
the pool's entries and are rarely emptied below 0.55. MRR keeps rising up
to 0.55-0.60 because dropping weak vector candidates lets the keyword
stream's answer rank higher, but each step loses answers from the vector
stream (97% at 0.55, 94% at 0.60 on 8B), so 0.45 stays the default.

**Near-duplicate warning (0.90) and entry-to-entry similarity, 8B.** Share
of the 256 version pairs whose old and new entries are at or above the
cut-off, of same-system distractors at or above it against either member,
and of background entries whose nearest other entry is at or above it:

| cut-off | versions | distinct same-system facts | background nearest neighbour |
|---|---|---|---|
| 0.85 | 92% | 12% | 1.5% |
| 0.90 | 83% | 3% | 0.1% |
| 0.95 | 59% | 1% | 0% |

The background inflation is gone (nearest neighbour at 0.85 or more: 39%
of the old pool, 1.5% now). 0.90 catches more versions than on the old
clean domains (83% against 71%) and flags 3% of distinct same-system facts
(1% before); the rewritten distractors share more wording with their pair
by design. The warning's wording (rate it, supersede it, or add see_also)
covers that 3%, so 0.90 stays.

**Not re-run:** the tau sweep (`make measure-versions`; grouping stays
off), and narrowed importance. The confirmation clock and per-kind
profiles were removed (below).

## The confirmation clock and per-kind recency are removed (ADR 0103)

Both off-by-default ranking options were measured a second time on
2026-10-21 with Qwen3-Embedding 8B, 4B and 0.6B and came out worse again.
The confirmation clock (ADR 0071, `HIVEMIND_RECENCY_FROM_CONFIRMATION`)
lowered pooled MRR by 0.11 on 8B and 0.09 on 4B and 0.6B. The per-kind
half-lives (ADR 0072, `HIVEMIND_RECENCY_BY_KIND`) cost about 0.04 MRR on
every model at `insight=180,decision=365`; at `insight=90,decision=180` they
were flat on 8B and worse on 4B and 0.6B. ADR 0103 removes both options and
their measurement variants; the default ranking is unchanged. The earlier
sections on ADR 0071 and ADR 0072 keep their results as history. The
shared harness is now `tests/eval/ranking.py` (`make measure-ranking`) and
measures the recency hint variants.

## LongMemEval, search only (2026-10-22 to 2026-10-26)

A public benchmark next to the synthetic corpus: LongMemEval_S, the 470
answerable questions (the 30 abstention questions are left out). Each
question comes with about 48 chat sessions; each session is stored as one
entry and the question searched with `SearchService` at limit 10, so the
metrics are session-level recall and nDCG@10, macro-averaged over
questions. There is no answering model and no judge, so the figures are
not comparable with end-to-end QA accuracy. The harness is
`tests/eval/longmemeval.py` (`make measure-longmemeval`); the embeddings
were computed on Kaggle's free T4 GPUs with `scripts/longmemeval-gpu/`
(fp16 `transformers`, matching the Q8_0 GGUFs to cosine ≥ 0.998). 8B used
dimension 2000, the largest the index allows, and 0.6B dimension 1024.

**Setup.** The 2.12.0 defaults (query instruction on, cut-off 0.45, no
recency hint) as "shipped"; then the `ignore_age` hint agents send, no
cut-off, the keyword stream alone, and no query instruction.

**Reproducibility.** Until PR #233 every seeded entry shared one
`created_at`, so score ties (frequent in `MemoryStore`'s word-overlap
keyword stream) fell through to random entry ids, and two runs on the same
vectors differed on about half the questions (overall nDCG@10 by up to
about 0.005). The harness now orders ties by a hash of the session id. The
8B figures below come from a run with that fix; the 0.6B figures predate
it.

**Overall, all 470 questions.**

| variant | model | recall_any@5 | recall_all@10 | nDCG@10 |
|---|---|---|---|---|
| shipped | 8B | 0.923 | 0.847 | 0.841 |
| `ignore_age` hint | 8B | 0.940 | 0.853 | 0.858 |
| no cut-off | 8B | 0.898 | 0.872 | 0.772 |
| keyword only | 8B | 0.781 | 0.717 | 0.614 |
| no query instruction | 8B | 0.900 | 0.821 | 0.815 |
| shipped | 0.6B | 0.872 | 0.787 | 0.782 |
| `ignore_age` hint | 0.6B | 0.917 | 0.809 | 0.817 |
| no cut-off | 0.6B | 0.898 | 0.868 | 0.771 |
| keyword only | 0.6B | 0.777 | 0.702 | 0.608 |

**nDCG@10 per question type.**

| type | n | 0.6B shipped | 8B shipped | 8B `ignore_age` | 8B no cut-off | 8B no instruction |
|---|---|---|---|---|---|---|
| knowledge-update | 72 | 0.887 | 0.909 | 0.935 | 0.856 | 0.881 |
| multi-session | 121 | 0.728 | 0.792 | 0.818 | 0.762 | 0.761 |
| single-session-assistant | 56 | 0.938 | 0.984 | 0.974 | 0.683 | 0.978 |
| single-session-preference | 30 | 0.608 | 0.688 | 0.675 | 0.493 | 0.626 |
| single-session-user | 64 | 0.711 | 0.806 | 0.878 | 0.734 | 0.794 |
| temporal-reasoning | 127 | 0.783 | 0.839 | 0.833 | 0.858 | 0.813 |

**Findings.**

1. 8B beats 0.6B on every question type, by 0.059 nDCG@10 overall.
2. The cut-off is a clear win on 8B: without it nDCG@10 drops from 0.841
   to 0.772, most where one session holds the answer (single-session
   assistant 0.984 against 0.683). Weak vector matches otherwise take part
   in the fusion and push the answer session down. The cost is some recall
   where the answer is spread over several long sessions (multi-session
   recall_all@10 0.702 with the cut-off, 0.777 without): a whole chat
   session is far longer than a Hivemind entry, and some score under 0.45
   against a short question. On 0.6B the cut-off is closer to neutral
   (nDCG@10 0.782 against 0.771). Every search returned 10 results either
   way; the keyword stream fills the list.
3. The query instruction (ADR 0097) helps on every question type: without
   it nDCG@10 drops from 0.841 to 0.815 and recall_all@10 from 0.847 to
   0.821, most on preferences (0.688 against 0.626), multi-session and
   knowledge-update. The gain is larger than on the rewritten synthetic
   corpus (MRR +0.005 there), as expected for short questions searched
   against long sessions.
4. The `ignore_age` hint helps overall (+0.017 nDCG@10 on 8B), most on
   single-session-user and knowledge-update, with small losses on three
   types.
5. The keyword stream alone is far behind (nDCG@10 0.614 on 8B).
6. The weak spots on both models are preferences (8B recall_any@10 0.83,
   nDCG@10 0.69) and finding every session of a multi-session question (8B
   recall_all@10 0.70).

**Decision.** No default changes. The cut-off stays at 0.45 and the query
instruction stays on. A search with no hint keeps the default recency
term: this benchmark puts answers in old sessions on purpose, which
favours `ignore_age`, while a live fleet also needs news to rise; agents
already send `ignore_age` (ADR 0101), and that is the row to compare with.
The 8B shipped row is the baseline for later ranking work.

Of the 18,064 session texts, 3 overflowed in fp16 and were embedded in
fp32, and 10 were too long for the GPU and were embedded from their first
half; none of the 10 is an answer session.
