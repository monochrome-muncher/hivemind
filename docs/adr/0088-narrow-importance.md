# A narrower importance factor, so one importance step stays below the peer cap

Amends [ADR 0022](0022-recency-floor-so-match-quality-is-the-sort-key.md) and [ADR 0070](0070-one-capped-budget-for-social-signals.md), and the importance factor in SPEC §6.4. On by default; `HIVEMIND_NARROW_IMPORTANCE=false` restores the old factor.

Status: Accepted. On by default after the measurement below.

## Context

The score is fused RRF × importance factor × recency × social (SPEC §6.4). The importance factor is `0.5 + 0.1 × importance`, 0.6 to 1.0, and the author alone sets it (ADR 0070 caps everything peers can say at `social_max`, 1.2).

Among near-equally relevant rivals the fused RRF score separates adjacent ranks by only about 1.6%, and rank 1 against rank 5 by 6.6%. The importance factor is far larger than that: importance 5 against 3 is 1.0 / 0.8 = **1.25x**, more than the **1.2** that all peer feedback together can add. So an author who rates an entry 5 outweighs a peer-confirmed entry at the quality ceiling, and importance, which agents tend to set high or at random, decides between equally relevant entries more than any evidence from readers does.

Invariant I1 (ADR 0070) is stated at equal importance, so it does not see this.

## Decision

1. **A narrower factor.** `entry_score` takes a keyword-only `narrow_importance: bool = False`. When true the importance factor is `1 + 0.05 × (importance - 3)`, so 1..5 gives 0.90, 0.95, 1.00, 1.05, 1.10. One importance step is about 1.05x, below `social_max`; the full range 1..5 is 1.1 / 0.9 = 1.22x. Importance 3, the default, is neutral (1.0). When false the factor is `0.5 + 0.1 × importance`, bit for bit.
2. **A setting.** `SearchConfig.narrow_importance`, from `HIVEMIND_NARROW_IMPORTANCE` (a `Settings` field, so the strict-env rule of ADRs 0024 and 0032 covers it), default `true`. `SearchService` passes it to `entry_score`. A deployment can set it `false` to get the old factor back. `entry_score` keeps `False` as its own default, like `recency_floor=None`: the shipped value is a configuration decision and the function stays a pure function of what it is handed.
3. **Invariant I6.** With the narrow factor on, one importance step is a smaller ratio than `social_max`, and an importance-5 entry does not outrank an importance-3 entry at the quality ceiling at equal match and age (1.1 against 1.2). Tested in `tests/unit/test_ranking_invariants.py` beside I1 to I5. I2 still holds: `narrow_importance` is a flag, not an author or rater identity, and joins the I2 parameter allowlist.
4. **Measured, then on.** `make measure-importance` (`tests/eval/importance.py`) runs the per-domain eval corpora with the configured embedder under three importance assignments (`equal`, `random`, `aligned`), each scored with the factor off and on, and reports hit@1, hit@5 and MRR per domain and pooled. The `aligned` assignment is the best case for the wide factor, where authors rate the answers higher; the narrow factor is expected to lose a little there and gain where importance is noise.

The importance field, its 1 to 5 range, the default of 3 and `importance_source` are unchanged. Entries that all have the same importance rank exactly as before; only differences in importance count for less.

## Measurement

Run on 2026-10-18 with Qwen3-Embedding 8B, 4B and 0.6B (1024 dimensions), over the sixteen domain corpora alone and pooled (646 queries). Cells are MRR, factor off against on; the weighted column weights 8B 70%, 4B 20%, 0.6B 10%.

| pooled, importance | 8B | 4B | 0.6B | weighted change |
|---|---|---|---|---|
| equal | 0.829 / 0.829 | 0.855 / 0.855 | 0.870 / 0.870 | +0.000 |
| random | 0.602 / 0.676 | 0.613 / 0.691 | 0.616 / 0.697 | +0.076 |
| aligned | 0.763 / 0.828 | 0.787 / 0.843 | 0.784 / 0.855 | +0.064 |

Pooled hit@1 and hit@5 rise on every model too (8B random: hit@1 0.458 to 0.542, hit@5 0.847 to 0.892). MRR rises in every domain on every model under both `random` and `aligned`. The only drops anywhere are single queries in hit@1 or hit@5 (one query in 40, 0.025) in six of the 96 domain rows, each with MRR still up. `equal` is identical, as it must be.

`aligned` was expected to favour the wide factor, and it did not. Under `aligned` every answer entry gets 4 or 5, including answers that are distractors for other queries, and the remaining entries draw 1 to 5, so a 5 on a near-miss still beats a 4 on the right answer by 1.25x under the wide factor. Importance is a weak signal of usefulness even when authors are honest, and the narrow factor keeps it as a tie-break. Full per-domain tables are in [docs/retrieval-experiments.md](../retrieval-experiments.md#a-narrower-importance-factor-adr-0088).

So the setting ships on. It changes ranking for any deployment whose entries have different importance values; `HIVEMIND_NARROW_IMPORTANCE=false` restores the old order, and `make measure-importance` re-measures with another embedder.
