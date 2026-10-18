# A write warns when its nearest entry is a near-duplicate

Amends [ADR 0052](0052-writes-report-the-nearest-entries.md) §3 (no floor) and [ADR 0077](0077-write-check-mode-and-warnings.md) §3 (the warning codes) and §4. Adds a row to SPEC §4.1. Does not refuse, merge or supersede anything (ADR 0001).

Status: Accepted. Always on, advisory only.

## Context

Every write already answers with `related`, its nearest active entries and their cosine `similarity` (ADR 0052), and the skill says to act on the nearest one. That is easy to skip: nothing in the reply says that one of them is almost the same text. The expanded-corpus measurement of 2026-10-17 (`docs/retrieval-experiments.md`) found that near-copied entries are what make version grouping misfire (ADR 0074), so keeping near-copies out of the pool is worth an explicit cue.

ADR 0052 §3 chose "no floor" because an untuned floor misfires. This decision adds a floor for a warning only. A false warning costs the writer a second look. It never costs a write, because nothing is refused.

## Decision

1. **`near_duplicate`** joins the advisory codes. It fires on writes and checks when the closest `related` entry has a cosine `similarity` of at least **0.90**, and its message names that entry.
2. **The message gives the three readings, not one remedy.** If it says the same, rate it instead of writing. If the new entry updates it, supersede it. If it is another point, link it with `see_also`. This matches the skill's table for the nearest entry. A fixed "supersede it" would hide a correct, distinct fact whenever the two only look alike, and it can name an entry the writer may not supersede (`may_supersede`, ADR 0033).
3. **Entries the draft names in `see_also` are skipped.** The writer has already said how they relate. An example is the skill's `self` note that links the fleet entry it localises.
4. **0.90 is measured on Qwen3-Embedding-8B, the production embedder.** Over the seven domains of the expanded corpus whose entries are not templated (112 version pairs), 0.90 catches 71% of the pairs where one entry updates the other. It catches 1% of the same-system distractors (a different setting of the same system). At 0.85 the figures are 84% and 7%, for example "the Airflow backfill pool has 2048 slots" against "the default pool has 128 slots" (0.883). At 0.95 they are 54% and 0%. The value is a constant tuned for Qwen3: another embedder places near-copies in a different band, so a deployment on another model should re-check it with the same pairs before relying on the warning.

## Consequences

- A careful writer sees the near-copy in the reply's `warnings` as well as in `related`. A writer that ignores warnings is no worse off than before.
- About three in ten updates sit below 0.90 and get no warning. `related` still lists them.
- The always-on text does not grow: the skill line that points at the warning replaces a longer one.
