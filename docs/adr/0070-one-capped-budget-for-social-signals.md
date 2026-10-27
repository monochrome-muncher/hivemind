# One capped budget for social signals

> **Amended by [ADR 0106](0106-confirmation-halves-older-flags.md):** I3 now reads that a confirmation's date moves no factor, and only whether one is newer than every flag counts, to halve those flags; I7 is added (halving stays between the full-flag quality and the helpful reports alone).

> **Amended by [ADR 0103](0103-remove-confirmation-clock-and-per-kind-recency.md):** I3 now reads "a confirmation date never reaches the score" and I4 is retired (there is no confirmation gain for a flag to cancel); I1, I2 and I5 stand, with every kind aging by one half-life. The notes below about 0071 and 0072 describe removed behaviour.

> **Amended by [ADR 0088](0088-narrow-importance.md):** by default (`HIVEMIND_NARROW_IMPORTANCE`, on), the importance factor narrows from 0.6..1.0 to 0.9..1.1, so one importance step (about 1.05x) is smaller than `social_max`; invariant I6 states it. I1 is unchanged and is still stated at equal importance.

> **Amended by [ADR 0073](0073-a-recency-hint-on-search.md):** I1 does not hold under the `ignore_age` recency hint, which a caller sends per search and which works only with `HIVEMIND_RECENCY_HINT` on. It holds under `default` and `prefer_recent`, and always with the setting off.

> **Amended by [ADR 0072](0072-recency-profile-per-kind.md):** with a recency profile on, I1's ages are per kind: the floor age is `h × log2(1/floor)` and the new-entry bound `h × log2(1/(social_max × floor))` for the kind's own half-life `h`. The shipped 9.7 and 1.7 days are the 30-day case.

> **Amended by [ADR 0071](0071-a-confirmation-can-restart-the-recency-clock.md):** the "last confirmed" date is the first signal to share this budget (the last factor is `min(social_max, quality × r_conf / r_occ)`, behind a setting that is off by default), and invariants I3 and I4 are now tested with it.

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
