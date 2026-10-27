# Remove the confirmation clock and the recency profile per kind

> **Amended by [ADR 0106](0106-confirmation-halves-older-flags.md):** `last_confirmed_at` is no longer display only. Its date still enters no score, but whether it is set halves the entry's flags in quality (on by default).

Supersedes [ADR 0071](0071-a-confirmation-can-restart-the-recency-clock.md) and [ADR 0072](0072-recency-profile-per-kind.md). Amends [ADR 0022](0022-recency-floor-so-match-quality-is-the-sort-key.md), [ADR 0070](0070-one-capped-budget-for-social-signals.md) (invariants I3 and I4), SPEC §4.2 and §6.4, and [ADR 0073](0073-a-recency-hint-on-search.md) (the hint no longer has a kind half-life or a confirmation restart to keep or drop).

Status: Accepted. Both options are removed; the default ranking is unchanged because both were off by default.

## Context

ADR 0071 let an entry's "last confirmed" date restart the recency clock for the upside (`HIVEMIND_RECENCY_FROM_CONFIRMATION`). ADR 0072 let a deployment give each kind its own half-life (`HIVEMIND_RECENCY_BY_KIND`). Both shipped off by default "until measured with the real embedders", and each carried an ongoing cost: a second scoring path in `entry_score`, a parse and validation layer, two environment variables, two Makefile-measured variants, and invariants I1, I3, I4 and I5 each tested with and without them.

## Measurement

Each option was measured twice on the five-domain fixture (160 entries aged 0 to 400 days) with Qwen3-Embedding 8B Q8_0, 4B and 0.6B f16, against the shipped ranking over the same pool, and came out worse both times.

**2026-10-11** (tables in `docs/retrieval-experiments.md`, the ADR 0071 and ADR 0072 sections). Pooled MRR, shipped to option:

| option | 8B | 4B | 0.6B |
|---|---|---|---|
| confirmation clock on | 0.737 to 0.650 | 0.706 to 0.639 | 0.683 to 0.611 |
| per kind `insight=180,decision=365` | 0.727 to 0.686 | 0.744 to 0.708 | 0.700 to 0.649 |
| per kind `insight=90,decision=180` | 0.727 to 0.719 | 0.744 to 0.744 | 0.700 to 0.696 |

Neither met ADR 0071's pass rule (no domain or pooled drop beyond 0.01) on any model; 90/180, the nearly neutral profile, still cost data analysis -0.078 and system administration -0.081 MRR.

**2026-10-21**, re-run with the query instruction on (ADR 0097) during the ADR 0101 measurement, same fixture and harness. Only the hint results were tabulated in `docs/retrieval-experiments.md`; the two options were again worse: the confirmation clock -0.11 pooled MRR on 8B and -0.09 on 4B and 0.6B, per kind 180/365 about -0.04 pooled on every model (data analysis -0.07 on 8B), and 90/180 flat on 8B and worse on 4B and 0.6B.

The confirmation clock lifts an old, confirmed entry over a newer one that is the better match, and the cost shows on every model. A longer half-life for insights and decisions keeps old entries near the top of queries whose answer is the newer entry. Neither option has a measurement to justify keeping it, and the measurement that ADR 0071 and 0072 asked for has now been made twice.

## Decision

1. **Both options are removed.** `SearchConfig.recency_from_confirmation`, `SearchConfig.recency_by_kind` and `half_life_for`, the `HIVEMIND_RECENCY_FROM_CONFIRMATION` and `HIVEMIND_RECENCY_BY_KIND` settings and their parsing, `RecencyTerms.use_confirmation`, and the `confirmed_at` argument of `entry_score` are gone. Every kind ages by `half_life_days`. `SearchConfig.recency_terms` takes the hint alone.
2. **Default ranking is bit-identical.** The removed paths ran only when a deployment turned them on. The recency hint (ADRs 0073, 0101) scores exactly as before: `prefer_recent` still lowers the floor, `ignore_age` still drops the recency factor.
3. **`last_confirmed_at` stays, as display only.** The hit and the read still carry it (SPEC §4.2), it still feeds the shown `correction` and the order inside a version group (ADR 0074), and it never enters a score.
4. **`entry_score` has no default for `narrow_importance`.** The function defaulted it to off while the configured default is on (ADR 0088). The parameter is now required and keyword-only, so a caller states which importance factor it scores with.
5. **A deployment that still sets either variable keeps starting.** The two names are in a retired-variable list in `config.py`: they are not `Settings` fields and not unknown, so `HIVEMIND_STRICT_ENV=true` does not fail on them. Startup logs one warning naming those present, and they change nothing.
6. **The harness.** `tests/eval/confirmation.py` becomes `tests/eval/ranking.py` and `make measure-confirmation` is dropped; `make measure-ranking` remains and measures the recency hint variants (ADR 0073) against the shipped ranking. The fixture is unchanged: the same entries, ages and the same 20% of entries with a helpful report and note are seeded, so `make measure-ranking` reproduces the recorded hint baseline (ADR 0101). Only the confirmation-clock and kind-profile variants, and the slices that measured only the two options, are deleted. Slow measurement harnesses stay out of the normal pytest run, and the retrieval eval gate is not re-pinned.

## Consequences

One scoring path, two fewer environment variables, and invariants I1 to I5 tested on the paths that exist (I3 and I4 were about the confirmation gain and are folded into the statement that a confirmation date never reaches a score). The historical results stay in `docs/retrieval-experiments.md` and the bodies of ADRs 0071 and 0072 are not rewritten. Reopening either idea needs a new ADR and a measurement that beats these. No migration, no schema change, no wire change.
