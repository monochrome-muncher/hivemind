# A confirmation can restart the recency clock

> **Superseded by [ADR 0103](0103-remove-confirmation-clock-and-per-kind-recency.md):** the setting and its scoring path were removed after the 2026-10-21 measurement with the Qwen3 embedders (pooled MRR -0.11 on 8B, -0.09 on 4B and 0.6B). The text below is the decision as it was made.

Amends [ADR 0022](0022-recency-floor-so-match-quality-is-the-sort-key.md) and [ADR 0070](0070-one-capped-budget-for-social-signals.md), and SPEC §6.4.

## Context

An entry's recency comes from its memory date (`occurred_at`) alone. A
two-year-old fact that an agent re-checked yesterday, and said what it
checked, ranks exactly like one nobody has looked at: past about 9.7 days
every entry carries the floor's 0.8 (ADR 0022), and a helpful report is
worth the same 1.067 whether it was made yesterday or two years ago
(scenario S2 in `docs/retrieval-experiments.md`: 0.853 against 0.800).
The "last confirmed" date (SPEC §4.2) already records the evidence, a noted
`helpful` report with no `stale` or `wrong` report since, but is only
shown. ADR 0070 reserved the shared social budget for exactly this signal.

## Decision

1. **A confirmation restarts the clock for the upside.** With the setting
   on, the score's last factor becomes

   ```
   r_occ  = max(floor, 0.5 ** (age(occurred_at) / half_life))
   r_conf = max(floor, 0.5 ** (age(max(occurred_at, last_confirmed_at)) / half_life))
   social = min(social_max, quality × r_conf / r_occ)
   final  = fused × importance_factor × r_occ × social
   ```

   `r_conf` equals `r_occ` without a confirmation, or with one that is not
   newer than `occurred_at`, so the ratio is 1. The cap still bounds only
   the upside: a quality penalty times a gain can stay below 1, as intended.
   A confirmation dated after the clock clamps to age zero, as a future
   `occurred_at` does; if `r_occ` underflows to 0 (the unbounded form, an
   absurd age) the ratio is taken as 1.

2. **Off by default.** `SearchConfig.recency_from_confirmation` /
   `HIVEMIND_RECENCY_FROM_CONFIRMATION`, default `false`. `entry_score` takes
   `confirmed_at` as a keyword-only argument whose `None` is bit-identical
   to before, and `SearchService` passes `last_confirmed_at` only when the
   setting is on. With it off no ranking changes.

3. **It stays on only if it is measured to help.** It is turned on by a
   deployment, and the default flips, only after
   `make measure-confirmation` (`tests/eval/confirmation.py`) has been run
   with the real embedders, 70% 8B / 20% 4B / 10% 0.6B, per domain and
   pooled, and:
   - no domain's or the pooled hit@5 or MRR drops by more than 0.01;
   - the aged-currency slice and S2 improve;
   - old-exact queries lose at most 0.02 MRR when confirmations are
     sprinkled on about 20% of entries.

## Guardrails

- The gain is at most `1 / recency_floor` = 1.25x before the cap, and the
  cap (`social_max`, ADR 0070) holds the product to 1.2, so an old
  confirmed entry reaches 0.8 × 1.2 = 0.96, below a new unrated entry's
  recency under about 1.7 days. Invariant I1 is tested with the setting on.
- It fades: a confirmation older than about 10 days (`0.5 ** (age / 30) <
  0.8`) is worth nothing more than the floor.
- It ignores how many agents confirmed: the gain is a function of the
  newest date alone (I3), so more confirmers add nothing. One non-author
  agent re-posting a noted `helpful` every few days can keep an entry at
  the cap; that is an accepted risk, bounded by the cap and by I1.
- A later `stale` or `wrong` report cancels it (I4): the date is none, so
  the entry is aged from its memory date again.
- A note is required, a report from the author does not count (ADR 0065),
  and a revoked reporter is left out (ADR 0067), all by the definition of
  "last confirmed".
- The invariants run against both settings; the allowlist of `entry_score`'s
  inputs gains one date and nothing else (I2).

## Consequences

- Nothing changes until a deployment sets the variable. Reading feedback,
  the displayed `last_confirmed_at` and the quality multiplier are as before.
- With the setting on, a confirmation is worth more than the helpful vote
  that made it: S2 goes from 0.853 against 0.800 to 0.960 against 0.800.
  It can also lift a confirmed older entry over a recent, closer match, and
  on the embedding-free slices it did so for sprinkled confirmations
  (`docs/retrieval-experiments.md`). The real-embedder run (2026-10-11,
  70/20/10) did not meet the pass rule: pooled MRR fell from 0.725 to
  0.644 and every domain fell, by 0.018 to 0.198. The setting stays off;
  a re-measure with confirmations that follow real use would be needed
  before a deployment turns it on.
- Later changes (per-kind recency, a search recency hint) add variants to
  the same measurement harness.
