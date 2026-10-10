# One capped budget for social signals

Amends [ADR 0022](0022-recency-floor-so-match-quality-is-the-sort-key.md) and [ADR 0063](0063-feedback-quality-by-consensus-share.md).

## Context

The age-varied study (`docs/retrieval-experiments.md`) found that past
about 9.7 days (`0.5 ** (age / 30) < 0.8`) every entry carries the same
recency factor, the floor of ADR 0022. From then on, feedback quality is
the only factor that separates two entries of equal match and importance.
More social signals are planned, a confirmation date that can restart an
entry's recency clock among them. Unbounded, they would let an old,
well-liked entry outrank a new one for good.

## Decision

1. **One cap on the upside.** The score's last factor is
   `min(social_max, quality)` (SPEC §6.4). `social_max` is
   `SearchConfig.social_max` / `HIVEMIND_SOCIAL_MAX`, default **1.2**,
   finite and `>= 1` (below 1 it would penalise unrated entries). It
   bounds only the upside: a penalty (`quality < 1`) passes through, and
   no feedback is still exactly 1.0. Later social signals share this
   budget rather than each adding a factor.

2. **No ranking change today.** 1.2 equals the default `quality_max`, so
   the cap is never reached by quality alone and every score is
   bit-identical to before (tested over a grid of fused scores,
   importances, ages and feedback counts). `entry_score` takes
   `social_max` as an optional keyword, `None` meaning no cap, like
   `recency_floor`, so the pure function stays a function of the numbers
   it is handed.

3. **Invariants every ranking change must pass**, tested against the
   shipped defaults in `tests/unit/test_ranking_invariants.py`:
   - **I1** At equal match and importance, a newly written unrated entry
     under about 1.7 days old is never outranked by one older than 10
     days, however many helpfuls or confirmations it has. The best an old
     entry can reach is 0.8 × 1.2 = 0.96, below `0.5 ** (1.7 / 30)` =
     0.9615. Raising the cap or the floor out of agreement fails the test.
   - **I2** Ranking inputs are numbers and dates only: no author, agent
     label, pin, link count or rater record. `entry_score`'s parameters
     are an allowlist in a test. This keeps ADRs 0057 (links), 0058
     (pins) and 0068 (rater flag counts) intact: they inform readers, not
     ranking.
   - **I3** A confirmation's gain depends only on the newest date.
   - **I4** A later stale or wrong report cancels a confirmation.
   - **I5** The upward multiplier never exceeds `social_max`, whatever
     `quality_max` is; penalties are not capped.

   I3 and I4 are tested with the confirmation date, which is not part of
   this change.

4. **Baselines, not gates.** Four scenarios (S1-S4, equal match unless
   stated) and the aged currency pairs are recorded in
   `docs/retrieval-experiments.md` with today's outcomes. Each says which
   later change is meant to flip it; that change edits the expected
   winner on purpose.

## Consequences

- Reading feedback is unchanged: `FeedbackService` and the readable
  summary report raw counts and the raw `quality`, not the capped factor.
  With the defaults they are the same number; if `quality_max` is raised
  above `social_max`, the reported quality can exceed what ranking uses.
- A deployment that raises `quality_max` above 1.2 gets no extra ranking
  effect until it raises `social_max` too, and I1 holds only at the
  shipped values. Keep both at 1.2 unless re-measured.
