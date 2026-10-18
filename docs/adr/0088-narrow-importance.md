# A narrower importance factor, so one importance step stays below the peer cap

Amends [ADR 0022](0022-recency-floor-so-match-quality-is-the-sort-key.md) and [ADR 0070](0070-one-capped-budget-for-social-signals.md), and the importance factor in SPEC §6.4. Behind a setting that is off by default.

Status: Accepted. Off until `make measure-importance` has been run with the deployment's embedder.

## Context

The score is fused RRF × importance factor × recency × social (SPEC §6.4). The importance factor is `0.5 + 0.1 × importance`, 0.6 to 1.0, and the author alone sets it (ADR 0070 caps everything peers can say at `social_max`, 1.2).

Among near-equally relevant rivals the fused RRF score separates adjacent ranks by only about 1.6%, and rank 1 against rank 5 by 6.6%. The importance factor is far larger than that: importance 5 against 3 is 1.0 / 0.8 = **1.25x**, more than the **1.2** that all peer feedback together can add. So an author who rates an entry 5 outweighs a peer-confirmed entry at the quality ceiling, and importance, which agents tend to set high or at random, decides between equally relevant entries more than any evidence from readers does.

Invariant I1 (ADR 0070) is stated at equal importance, so it does not see this.

## Decision

1. **A narrower factor.** `entry_score` takes a keyword-only `narrow_importance: bool = False`. When true the importance factor is `1 + 0.05 × (importance - 3)`, so 1..5 gives 0.90, 0.95, 1.00, 1.05, 1.10. One importance step is about 1.05x, below `social_max`; the full range 1..5 is 1.1 / 0.9 = 1.22x. Importance 3, the default, is neutral (1.0). When false the factor is `0.5 + 0.1 × importance`, bit for bit.
2. **A setting.** `SearchConfig.narrow_importance`, from `HIVEMIND_NARROW_IMPORTANCE` (a `Settings` field, so the strict-env rule of ADRs 0024 and 0032 covers it), default `false`. `SearchService` passes it to `entry_score`. A deployment can set it `false` again if the narrow factor later becomes the default.
3. **Invariant I6.** With the narrow factor on, one importance step is a smaller ratio than `social_max`, and an importance-5 entry does not outrank an importance-3 entry at the quality ceiling at equal match and age (1.1 against 1.2). Tested in `tests/unit/test_ranking_invariants.py` beside I1 to I5. I2 still holds: `narrow_importance` is a flag, not an author or rater identity, and joins the I2 parameter allowlist.
4. **Off until measured.** Like ADRs 0071 to 0073, the setting ships off. `make measure-importance` (`tests/eval/importance.py`) runs the per-domain eval corpora with the configured embedder under three importance assignments (`equal`, `random`, `aligned`), each scored with the factor off and on, and reports hit@1, hit@5 and MRR per domain and pooled. The `aligned` assignment is the best case for the wide factor, where authors rate the answers higher; the narrow factor is expected to lose a little there and gain where importance is noise.

The importance field, its 1 to 5 range, the default of 3 and `importance_source` are unchanged. Nothing is re-ranked unless the setting is on.

## Measurement

Results: pending
