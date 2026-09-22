# A floor under the recency factor, so match quality is the sort key

**Amends SPEC §6.4** (the decay-aware rescore). ADR 0006's hybrid shape —
RRF fusion, then re-score by importance × recency × feedback quality — is
unchanged; what changes is that the recency factor is now **bounded
below**, at a default of **0.8**.

SPEC §6.4 multiplies every fused hit by `0.5 ** (age_days /
half_life_days)`. That term was written as a tie-break: "a fresh,
important, well-remembered entry beats a slightly-more-similar but stale
one." It has never behaved as one, and the reason is arithmetic, not a
property of any corpus.

**The fused score is compressed; the recency factor is not.** With the
SPEC §6.2 defaults (`rrf_k=60`, `w=0.5/0.5`, `candidate_top_k=20`) the
best possible candidate scores `1/(k+1)` and the worst retained one
`0.5/(k+20)`, so the *entire* fused range across a candidate list is

    2(k + 20) / (k + 1)  =  2.6230x

while `0.5 ** (age/half_life)` is unbounded below — 2x per half-life,
forever. So ~42 days of age difference (~12 days when both candidates
appear in both retrieval streams, where the fused spread is only 1.31x)
outranks *any* match-quality difference, however large. Recency was the
sort key and match quality the tie-break: the exact inverse of §6.4's
stated intent. This is arithmetic on the two formulas and holds for any
candidate list, synthetic or real.

The measured symptom (ROADMAP §4.4's age-varied fixture): on the
"old entry matches almost verbatim, recent entry shares a phrase"
pattern, the shipped pipeline scored **0.000 hit@5** — for the temporal
and the non-temporal query alike — where the same pipeline with decay
switched off scored 1.000.

## Decision

1. **The recency factor is floored.** SPEC §6.4 becomes

       max(recency_floor, 0.5 ** (age_days / half_life_days))

   with `recency_floor` a `SearchConfig` value (`HIVEMIND_RECENCY_FLOOR`),
   **default 0.8**, validated to `(0, 1]` at construction. `None` (the
   pre-ADR-0022 unbounded form) remains expressible and is what every
   §4.4/§4.6 measurement is taken against.

2. **The floor is chosen so that `1/floor` fits inside the fused range.**
   A floor `f` collapses the recency factor's range from `2 ** (age spread
   / half_life)` to exactly `1/f`. At 0.8 that is **1.25x**, comfortably
   inside 2.6230x — so match quality is the sort key and recency the
   tie-break, which is what §6.4 always said. This condition is pinned as
   a test against the *shipped* config, not against a fixture, so it
   fails if the floor is lowered or `rrf_k` raised out of agreement.

3. **`entry_score`'s own parameter default stays `None`.** The pure
   function is a function of the numbers it is handed; the value is a
   configuration decision and lives in `SearchConfig` (and `Settings`,
   which feeds it). A test pins both to 0.8 and a second pins that the
   knob actually reaches the scorer through `SearchService` — the inert-knob
   failure mode ADR 0021 found on the embedding prefix budget.

## Why 0.8, and how much of that number to trust

**The threshold was predicted before the measurement, from the pipeline's
own constants.** Match quality can begin to dominate only once `1/f` is
narrower than the fused range, i.e. **`f > 1/2.6230 = 0.381`**. That
prediction was recorded before the sweep ran, and it was falsifiable: a
floor at or below 0.381 had to do nothing.

The sweep (`tests/eval/test_decay_experiment.py::TestRecencyFloorSweep`,
decay ON, `rrf_k=60`, k=5; full table in ROADMAP §4.6) matched its shape
exactly — flat at and below 0.381, onset just above, complete by 0.8:

| floor | `old_exact` MRR (always-on: 0.000) | `currency_pair` MRR (decay-off: 0.611) |
|---|---|---|
| 0.2 | 0.100 | 0.833 |
| 0.381 | 0.100 | 0.833 |
| 0.5 | 0.500 | 0.778 |
| 0.7 | 0.750 | 0.778 |
| **0.8** | **1.000** | **0.778** |
| 0.9 | 1.000 | **0.583** — below decay-off |

A confirmed pre-registered prediction is the strongest evidence in this
whole area, and **it does not depend on the embedder**: it is a statement
about two formulas. 0.8 weakly dominates its neighbour — 0.7 and 0.8 tie
exactly on `currency_pair` (0.778) and 0.8 wins `old_exact` (1.000 vs.
0.750) — so there is no robustness argument for hedging to 0.7.

**A floor is a band, not a slider, and the operational rule is: do not
tune it up.** The curve is non-monotone. At 0.9 the recency range is
1.11x, narrower than the fused spread of almost any pair, so the variant
is *nearly* decay-off and pays decay-off's price: `currency_pair` MRR
falls to 0.583, **below** switching decay off entirely (0.611). At 1.0 it
**is** decay-off. An operator who reads "higher floor = better recall for
old entries" and raises it toward 1.0 is not tuning the term, they are
deleting it — and they will pass through a region that is worse than
either end. This is the non-obvious fact this ADR exists to record.

## What it costs

**This is not a pure win, and it should not be described as one.** On the
`currency_pair` slice — "which of these two entries is the current one?",
the queries the recency term exists to answer — MRR goes **0.833
(always-on) → 0.778 (floor 0.8)**. The trade is losing 0.055 there to gain
`old_exact` **0.000 → 1.000**, plus non-temporal MRR 0.208 → 0.653 and
hit@5 on all ten queries 0.500 → 1.000.

A second honest number: on the **all-query** aggregate, decay-off still
beats floor 0.8 (MRR 0.833 vs. 0.725) on this fixture. The floor is not
chosen because it wins that aggregate; it is chosen because switching
decay off costs the `currency_pair` slice (0.833 → 0.611) and an org-wide
memory pool must be able to answer "what is the current value of X".
Ten hand-written queries do not weight that slice the way a real pool
would, and the aggregate over them is not the objective.

## The value is provisional; the shape is not

The *mechanism* and the *sign* are established by arithmetic and hold for
any corpus: `1/f` is the entire recency range, and there is no analogue of
the ceiling that killed the `rrf_k` lever. The **constant 0.8** is not:
it comes from 16 synthetic entries and 10 hand-written queries scored by a
**4-dimension hash embedder** (`tests.fakes.FakeEmbedder`). That fixture
cannot locate an optimum — the 0.5–0.8 band is barely separated on it, and
`timeless` MRR keeps rising past 0.8 while `currency_pair` falls, so the
optimum is a trade between competition patterns, not a peak.

**Trigger to re-measure:** the first real corpus with meaningful age
spread — run the §1.1 harness against real queries on an aged production
pool. Re-measure the floor *then*, sweeping the 0.5–0.9 band, and expect
to move the value, not the form. Until then the default is a measured
choice, not a tuned one, and it is one config value to change.

## Consequences

* **Rankings change on any pool with age spread.** Old, exactly-matching
  entries that were buried now surface; a recent, loosely-matching entry
  no longer beats them on age alone. On a pool where everything shares a
  timestamp nothing changes at all — which is why the ROADMAP §1.1 eval
  gate is **bit-identical** before and after this change (every golden
  entry is seeded without `occurred_at`, so every recency factor is 1.0
  and a lower bound on 1.0 is a no-op). The gate's pinned thresholds were
  **not** re-pinned, and a test now asserts that equality rather than
  arguing it, so a future golden set that gains age spread fails loudly
  instead of letting the thresholds quietly absorb a scoring change.
* **No migration, no re-embedding, no schema change.** This is a scoring
  change only; stored entries and vectors are untouched.
* **`HIVEMIND_RECENCY_FLOOR` takes a value in `(0, 1]` and nothing else.**
  There is deliberately no env spelling for "no floor": the unbounded form
  is the defect this ADR fixes, and it is reachable only in code (passing
  `None`), which is what the measurements do. An operator who needs the
  old behaviour back sets a negligible floor (`1e-9` is 30 half-lives of
  range); one who wants no recency term at all sets `1.0`. Only the band
  between 0.381 and ~0.9 is a *tuning* decision.
* **ROADMAP §4.6 direction (a) is closed, which rules out (b).** §4.6
  states that the floor and the additive form are competing answers to
  "stop recency being the sort key" and must not be stacked — a floored
  multiplicative term plus an additive term is untunable. (b) is therefore
  **not** a follow-up to this change; reopening it means replacing the
  floor, under a new ADR. *Per-kind half-lives* is orthogonal, is not a fix
  for this finding, and stays held.
* **`rrf_k` stays at 60.** This change does not depend on it, but the two
  are coupled through the same inequality: raising `rrf_k` narrows the
  fused range and therefore raises the floor needed to satisfy
  `1/f < 2(k+20)/(k+1)`. The pinned test fails if they drift apart.

## Alternatives considered

* **Gate the recency term to temporal queries** (ROADMAP §4.4 — measured,
  **rejected**). The hypothesis was that an always-on decay only hurts
  queries with no time sense. Measured against an *oracle* label the
  fixture supplies (not a classifier the service has), the gated variant
  lost to simply switching decay off on **every** aggregate (all-query MRR
  0.800 vs. 0.833, hit@5 0.900 vs. 1.000). And slicing by competition
  pattern rather than by time sense showed why: the failure appears on the
  temporal *and* the non-temporal query of the same pattern, so time sense
  is not the axis that predicts where the term hurts. Gating narrows where
  the defect shows; it does not fix it.
* **Lower `rrf_k` to widen the fused range** (ROADMAP §4.6 — measured,
  **rejected, and it cannot work at all**). This attacks the same mismatch
  from the compressed side and is the cheapest possible lever (a plain
  config value, no SPEC change). Sweeping {60, 20, 10, 5} left the
  `old_exact` slice at **0.000 hit@5 at every `k`**. The reason is a bound,
  not a fixture: `2(k+20)/(k+1)` tends to **40x** as `k → 0`, so the entire
  lever is worth at most `log2(40) = 5.3` half-lives — ~160 days at the
  30-day default — against entries 12.7 and 14 half-lives old. No `k`
  closes a gap of that size, for any corpus.
* **The additive recency term** (ROADMAP §4.6 direction (b)) — recency
  competing on the same scale as the fused score instead of multiplying
  it. It is a real alternative to this ADR and it addresses the same
  defect, but it is a larger redesign of §6.4 and it is **mutually
  exclusive** with the floor. The floor was measurable in one config
  value against an existing fixture; that decided the order, and landing
  it closes (b) as described above.
* **Delete the recency term** (`recency_floor = 1.0`, i.e. decay-off).
  It wins the all-query aggregate on the fixture, and it is the honest
  competitor. Rejected because it costs the `currency_pair` slice (MRR
  0.833 → 0.611): an org-wide agent memory that cannot prefer the current
  value of a fact over a superseded-in-spirit one has lost something SPEC
  §6.4 deliberately bought. The floor keeps 0.778 there.
* **Raise the floor above 0.8** — see "a band, not a slider": measured
  *worse* than decay-off at 0.9 on the slice the term exists for.
