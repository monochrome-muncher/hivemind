# Feedback quality by consensus share

> **Amended by [ADR 0106](0106-confirmation-halves-older-flags.md):** by default (`HIVEMIND_CONFIRMATION_HALVES_FLAGS`, on), once an entry has a "last confirmed" date its `stale` and `wrong` reports count at half weight, in the share and in `n`.

> **Amended by [ADR 0070](0070-one-capped-budget-for-social-signals.md):** the score uses `min(social_max, quality)` (default 1.2, equal to `quality_max`). `quality` itself, and what feedback reports, are unchanged.

## Context

Feedback feeds a `quality` multiplier on an entry's retrieval score
(SPEC §4.2). It was `clamp(1 + 0.05·h − 0.10·s − 0.25·w, 0.5, 1.2)`. The
clamp comes after the sum, so helpful reports past the fourth are banked:
they push the sum far above the maximum, and later wrong reports have to
cancel that surplus before they show.

| helpful | stale | wrong | old quality | new quality |
|---|---|---|---|---|
| 4 | 0 | 0 | 1.2 | 1.133 |
| 20 | 0 | 0 | 1.2 | 1.182 |
| 20 | 0 | 3 | 1.2 | 1.04 |
| 5 | 0 | 1 | 1.2 | 1.0 |
| 0 | 0 | 1 | 0.75 | 0.667 |

An entry with 20 helpful and 3 wrong reports ranked as high as one with 20
helpful and none, and three wrong reports could not lower it at all.

## Decision

1. **A consensus share.** With `n = helpful + stale + wrong`:

   ```
   quality = clamp( 1 + (wh·h − ws·s − ww·w) / (n + prior), min, max )
   ```

   The defaults are `wh = 0.2`, `ws = 0.4`, `ww = 1.0`, `prior = 2`,
   `min = 0.5`, `max = 1.2`. No feedback gives exactly 1.0.

2. **Weights are end points.** Each weight is where quality heads when
   every report gives that verdict: all helpful tends to 1.2, all stale to
   0.6, all wrong to 0 (clamped to 0.5). The result depends on the share of
   each verdict, not on a running total, so reports cannot be banked.

3. **A prior.** `prior` counts imaginary neutral reports, so one report
   cannot swing quality far and more reports move it toward the majority
   verdict. It must be greater than 0.

4. **Config.** `SearchConfig.quality_prior` (`HIVEMIND_QUALITY_PRIOR`,
   finite and > 0, checked at startup per ADR 0024) joins the other
   `quality_*` values. `feedback_quality` takes it as `prior`, and
   `SearchConfig.quality_kwargs()` carries it to both search and the
   quality `FeedbackService` reports.

## Consequences

- Existing deployments that set `HIVEMIND_QUALITY_*_WEIGHT` get a
  different meaning: the weights are now per-verdict end points, not
  per-report steps. Operators should unset them or re-derive them.
- One wrong report alone now costs more (0.667 against 0.75) and one
  helpful report gives more (1.067 against 1.05).
- The formula amends the values in SPEC §4.2; the way quality enters the
  score (SPEC §6.4) is unchanged.
- The retrieval eval sets no feedback, so it is unaffected.
