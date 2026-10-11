# A recency profile per kind

Amends [ADR 0022](0022-recency-floor-so-match-quality-is-the-sort-key.md) and SPEC §6.4. Extends invariant I1 of [ADR 0070](0070-one-capped-budget-for-social-signals.md) per kind.

## Context

Every kind shares one recency curve: a 30-day half-life under a 0.8 floor
(ADR 0022). A decision ("we use a 90-day churn window") or an insight ("the
batch job is slow on Mondays because of the backup") does not go out of date
the way a fact ("the API is on v3") does, so ageing all three alike treats
the slow-moving kinds as stale about as fast as the fast-moving one. The idea
was that decisions and insights age slowly or not at all, and facts keep
today's curve.

## Decision

1. **A half-life per kind.** `SearchConfig.recency_by_kind`, a tuple of
   `(Kind, days)` pairs (a frozen, hashable form), `HIVEMIND_RECENCY_BY_KIND`,
   written `insight=180,decision=365`. A kind not named keeps
   `half_life_days`. `SearchConfig.half_life_for(kind)` returns the
   half-life, and `SearchService` passes it to `entry_score` in the place of
   `half_life_days`: `entry_score` gains no parameter (invariant I2 is
   unchanged; the kind reaches the score only as a number).
   Unset (the default), empty or `none` is `None`: every kind uses
   `half_life_days`, and the scores are bit-identical to before (tested).

2. **Validated at config time (ADR 0024).** Unknown kinds, a kind named
   twice, a missing or non-numeric value, and a value that is not finite and
   `> 0` are errors at startup, with the setting's name in the message. Kinds
   are case-insensitive and whitespace around names and numbers is ignored.

3. **The floor is the same for every kind (0.8).** It is a band, not a
   slider (ADR 0022).

4. **"No decay" is not offered; "slowly" is.** With no decay an old entry
   with applause would beat a new one forever: at equal match its recency
   stays 1.0, so the social factor (up to `social_max` = 1.2) puts it over a
   new unrated entry (1.0) at any age. That breaks I1, "a new answer beats old
   applause". A long half-life keeps the property, because past the kind's
   floor age the old entry's recency is again the floor.

5. **I1 per kind.** With half-life `h` for a kind, the floor age is
   `h × log2(1 / floor)` (9.66 days at `h = 30`) and a new unrated entry is
   never outranked, at equal match and importance, by an entry of any
   kind that is past its own kind's floor age, however much feedback it has,
   as long as it is younger than `h × log2(1 / (social_max × floor))` (1.77 days at 30):

   | half-life | new entry younger than | old entry past |
   |---|---|---|
   | 30 | 1.77 days | 9.66 days |
   | 90 | 5.3 days | 29 days |
   | 180 | 10.6 days | 58 days |
   | 365 | 21.5 days | 117.5 days |

   It is satisfiable at all only while `social_max × floor < 1` (0.96), which
   a test checks. `tests/unit/test_ranking_invariants.py` runs I1 over the
   configured profiles and a sweep of half-lives from 7 to 3650 days, and
   runs I2, I3, I4 and I5 with a profile on.

6. **Off by default, and measured before it is turned on.** The pass rule is
   ADR 0071's: no domain's or the pooled hit@5 or MRR drops by more than
   0.01, and the timeless slice of the age-varied fixture improves.
   `make measure-ranking` (the same command as `make measure-confirmation`)
   runs the variants "kind profile: insight=180, decision=365" and
   "insight=90, decision=180" over the domain fixtures
   (`tests/eval/confirmation.py`), and `tests/eval/test_kind_profile.py`
   covers the age-varied fixture's slices.

## Consequences

- **Across kinds the order changes, on purpose.** With a profile on, an old
  decision can outrank a same-age fact at equal match: at 20 days a fact's
  recency is the 0.8 floor and a decision's (365 days) is 0.963, a gap of at
  most `1 / floor` = 1.25x, since the floor bounds it. I1 holds within a kind and across kinds (a new entry of kind A under A's bound has a recency of at least 0.96, and any entry past its own kind's floor age scores at most 0.8 × 1.2 = 0.96);
  between kinds the longer half-life is the whole point and the cost.
- A superseded entry keeps its own kind's half-life (supersession does not
  change the kind of the old entry). The supersession invariant (SPEC §6.3)
  is applied after scoring and is unaffected.
- A confirmation (ADR 0071) restarts the clock with the same kind's half-life
  for both `r_occ` and `r_conf`, so the gain stays at most `1 / floor` and the
  cap still holds.
- Nothing changes until a deployment sets the variable. Measured numbers are
  in `docs/retrieval-experiments.md`; with the hash embedder the pass rule is
  not met and the timeless slice does not improve, which is machinery only.
  The real-embedder run decides, and until it is met the default stays off.
- **Real-embedder result (2026-10-11; Qwen3-Embedding 8B, 4B, 0.6B,
  weighted 70/20/10).** The pass rule is not met by either profile. Pooled
  hit@5 / MRR: baseline 0.894 / 0.728, insight=180,decision=365 0.878 /
  0.687, insight=90,decision=180 0.895 / 0.722. The 90/180 profile is nearly
  neutral pooled (-0.006 MRR) but costs data analysis -0.078 and system
  administration -0.081 MRR. Caveat: this fixture's queries do not favour old
  decisions or insights (ages are a deterministic spread, unrelated to which
  answers are timeless), so it measures the cost of the profile, not the
  benefit it is meant to bring (timeless knowledge not being buried); a
  fixture with timeless old decisions would be needed to show the benefit.
  The setting stays off.
