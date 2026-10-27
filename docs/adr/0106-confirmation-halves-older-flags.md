# A confirmation newer than every flag halves those flags

Amends [ADR 0063](0063-feedback-quality-by-consensus-share.md) (the quality formula), [ADR 0070](0070-one-capped-budget-for-social-signals.md) and [ADR 0103](0103-remove-confirmation-clock-and-per-kind-recency.md) (invariant I3, and a new I7), and SPEC §4.2 and §6.4. On by default; `HIVEMIND_CONFIRMATION_HALVES_FLAGS=false` counts every flag in full again.

Status: Accepted. On by default after the simulation below.

## Context

An entry's "last confirmed" date (SPEC §4.2) is set when a `helpful` report with a note is newer than every `stale` or `wrong` report on it. A hit already treats that as an answer to the flags: the newest correction note is hidden once a newer noted helpful exists (SPEC §6.1). Ranking did not. Quality counted every flag in full however many readers had since tested the entry and said it worked: one `wrong` plus one noted `helpful` gave 0.80, below an unrated rival, and it took five `helpful` reports to cancel one `wrong`. So the display said the flag was answered while the score still treated it as standing.

The 2026-10-18 feedback dynamics review (proposal B) found that among near-equally relevant rivals any penalty decides the order, and only someone actually trying the flagged entry can rescue it. Proposal B lets that rescue count for something. Its sibling proposal E (a bare `helpful` at half weight) was not taken: in production every helpful verdict carries a note, so it would change nothing.

## Decision

1. **Half weight for confirmed flags.** `feedback_quality` takes a keyword `flag_scale` (default 1.0): what each `stale` and `wrong` report counts as, both in the share and in `n`. The callers pass `CONFIRMED_FLAG_SCALE` (0.5) when the entry has a "last confirmed" date, and 1.0 otherwise:

   ```
   f = 0.5 if last_confirmed_at is set, else 1
   n = helpful + f·(stale + wrong)
   quality = clamp( 1 + (0.2·helpful − f·(0.4·stale + 1.0·wrong)) / (n + 2), 0.5, 1.2 )
   ```

   A flag newer than the confirmation clears "last confirmed", so every flag counts in full again; a tie is no confirmation. A bare `helpful` is no confirmation. With `flag_scale` 1.0 the formula is the old one, bit for bit.
2. **A setting.** `SearchConfig.confirmation_halves_flags`, from `HIVEMIND_CONFIRMATION_HALVES_FLAGS` (a `Settings` field, so strict-env covers it), default `true`. `SearchConfig.quality_kwargs(confirmed=...)` takes whether the entry is confirmed as a required keyword, so each caller states whether it knows. Search uses the `last_confirmed_at` it already reads; the quality a `hive_feedback` or `POST /v1/feedback` reply reports now reads the same signals instead of the bare counts, so it stays the number search uses. No new store method, no migration, no wire change.
3. **Invariants.** I3 (ADR 0103) now reads: a confirmation's date never reaches the score, its value moves no factor; only whether one exists, newer than every flag, does, and only to halve those flags. The 0103 measurement does not apply here: that option restarted the recency clock from the confirmation date, which lifted old confirmed entries over newer, better matches on every model. Here no date enters the score and nothing rises above the helpful reports alone. New **I7**: halving never lowers quality, and never lifts it above the quality the same helpful reports would give with no flags, so I1 and I5 hold unchanged. Tested in `tests/unit/test_ranking_invariants.py`, and through the service in `tests/unit/test_confirmation_halves_flags.py`.
4. **Social scenario S5.** One `wrong`, then three noted `helpful`, against an unrated rival of the same age: the rebutted entry now wins (quality 1.018 against 1.0); with flags in full it loses (0.933). `EXPECTED_WINNER` records the flip.

## Measurement

The retrieval corpora and LongMemEval have no feedback that builds up over time, so they cannot measure this. `make measure-flags` (`tests/eval/flag_dynamics.py`) simulates it through the shipped `feedback_quality` and `entry_score`: three rivals for one question at about equal match, one search a day for 60 days, 2,000 runs per cell. Agents use the top hit, or with `explore` a random other hit; they rate half their uses; every helpful carries a note, as production shows; a success is falsely flagged 3% of the time, and a failure is rated helpful with the `herding` share. Run on 2026-10-27; "best" is the best entry's share of uses, "failed" the share of uses that failed.

| scenario | explore, herding | flags in full | halved |
|---|---|---|---|
| false `wrong` on the best (.9 vs .7, .7) | 0%, 10% | best 1.2%, failed 29.7% | best 1.2%, failed 29.7% |
| | 20%, 10% | best 16.1%, failed 26.8% | best 21.9%, failed 25.7% |
| | 20%, 30% | best 13.1%, failed 27.4% | best 18.7%, failed 26.3% |
| false `stale` on the best | 20%, 10% | best 31.7%, failed 23.7% | best 34.6%, failed 23.2% |
| true `wrong` on a bad entry (.3 vs .7, .7) | 0%, 10% | failed 30.1% | failed 30.1% |
| | 20%, 10% | failed 34.3% | failed 34.5% |
| | 20%, 30% | failed 34.2% | failed 34.4% |
| control, no flags | 20%, 10% | failed 19.6% | failed 19.8% |

When agents only ever use the top hit, nothing changes: nobody tries the flagged entry again, so nothing confirms it. When they sometimes look further, a falsely flagged entry recovers faster and fewer uses fail. When the flag is true, failed uses rise by at most 0.2 points, within run-to-run noise, even with heavy herding: a herded helpful on a bad entry is soon followed by another flag, which restores full weight. The gain is modest and the risk small, so the setting ships on.

The simulation shows direction and rough size, not a forecast. Real agents open several hits and read the correction notes, so they sit nearer the `explore` rows than the greedy ones.

## Consequences

Ranking changes on upgrade only for entries that have a flag and a newer noted helpful; everything else scores bit for bit as before. The flag backlog (ADR 0093), `contested`, the shown correction and every count are unchanged: halving is in the score only, and an entry with a rebutted flag still appears in the backlog for someone to look at. Two reporters can trade verdicts on one entry, each flip moving it between half and full weight; that is bounded by the same quality range as before, and visible in the feedback rows.
