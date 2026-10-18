# Fleet health metrics: admin-only and observation-only

> **Extended by [ADR 0089](0089-owner-level-rating-metrics.md):** sibling-verdict and owner-concentration figures join the `health` section. Everything below stands.

Extends [ADR 0068](0068-rater-flag-confirmation-metric.md) and
[ADR 0069](0069-cache-the-entry-scan-behind-the-usage-report.md).

## Context

The usage report counts what is in the pool, not whether the pool is
getting better. Operators want to know whether verdicts carry evidence,
whether errors are corrected quickly, whether flagged entries have an
owner, whether old knowledge is ever re-checked, whether one author or one
reporter dominates, and whether entries are being combined. Each of these
is a number the pool can already produce.

Any such number can become a target (Goodhart): an agent that learns "helpful
verdicts with a note are good" starts writing notes to please the metric,
and the metric stops measuring anything. The same holds if the numbers feed
ranking: they would reward whatever is easiest to fake.

## Decision

1. **A `health` section in the usage report**, served by
   `MetricsService.usage_report()` to the admin key at `GET /v1/metrics`
   (additive `health` field), to the scrape target as `hivemind_health_*`
   gauges, and in the admin panel (a "Fleet health" card on the overview, a
   few per-fleet columns on the fleets view).

2. **Observation only.** No agent-facing surface returns these numbers and
   nothing reads them: not search, ranking, scoring, the `quality`
   multiplier (ADRs 0051, 0063), the briefing (ADR 0058), `hive_whoami` or
   any MCP tool. A unit test fails if a module outside the metrics service
   and the `Store` adapters touches the aggregates.

3. **The metrics** (all counts live pool state unless a window is named;
   revoked reporters' verdicts are left out, ADR 0067; a share is `null`
   while its denominator is 0; windows are counted in hours back from the
   report's `now`, up to `now`, so they do not depend on the server
   TimeZone):

   | Key | Meaning |
   |---|---|
   | `helpful_with_note_share` | current `helpful` verdicts whose note is not blank, of all current `helpful` verdicts |
   | `sourced_share` | active org- and fleet-scope facts and insights with at least one source, of all such entries |
   | `correction_latency_p50_seconds`, `corrections_measured` | median seconds from an entry's first standing stale/wrong verdict to its successor's creation, over entries superseded in the last 90 days that were flagged before the successor existed; the sample size |
   | `flagged_backlog_14d` | active entries whose oldest standing stale/wrong verdict is over 14 days old |
   | `orphaned_flagged` | active entries with a standing stale/wrong verdict whose author is a revoked agent or has written nothing for 30 days |
   | `floating_gap` | active entries with `occurred_at` over 90 days old and no verdict (from a non-revoked reporter) in the last 90 days. The age is the memory date on purpose: the question is whether knowledge about something old has been checked lately, not when it was written |
   | `top3_author_write_share`, `top3_reporter_verdict_share` | the three busiest authors' share of the last 90 days' writes (all states), and the three busiest reporters' share of the last 90 days' verdicts; with the number of distinct authors / reporters |
   | `synthesis_rate`, `self_to_fleet_promotions` | active insights with at least two `see_also` links, of all active insights; fleet entries written in the last 90 days that supersede a `self` entry |
   | `gap_tag_counts` | active entries tagged `anomaly`, `open-question`, `dead-end` |
   | `supersessions_per_100_writes` | entries superseded in the last 90 days (by the successor's creation time) per 100 entries written in them |
   | `echo_rate` | of the newest 200 writes of the last 30 days that have an embedding, the share whose nearest earlier active entry (any scope or fleet: the admin's view of the whole pool) has cosine similarity >= 0.9 |
   | `topic_dispersion` | for the 10 busiest tags (distinct tags of org- and fleet-scope entries with an embedding in the last 60 days; self-scope entries are not read, and an entry listing a tag twice counts once; ties by code-point order) that have at least 5 entries in each 30-day window: the mean pairwise cosine of the 50 newest entries of the tag written in the last 30 days and in the 30 days before, and their difference (`change`); a rise means writers are converging |
   | `rival_lifetime` | count and median age (seconds since `created_at`) of active entries tagged `alternative`, of active entries whose summary starts `Conflict:` (case-sensitive), and of contested entries (active, with a live helpful verdict beside a live stale/wrong one). Zero alternatives suggests premature closure, a growing pile that nobody tests them |
   | `grounding_share` | of fleet- and org-scope insights and decisions written in the last 30 days, the share with at least one source that is not an entry id. `sources` has no entry type (ADR 0057), so a source whose `ref` is only a UUID (optionally `entry:`-prefixed) is taken as an entry link; anything else counts as external |
   | `top3_agent_label_write_share`, `top3_agent_label_verdict_share` | the three busiest `agent` labels' share of the last 30 days' writes, and of the last 30 days' verdicts, with the number of labels. Blank labels are left out of both numerator and denominator. The label is self-reported on a user key and fixed by an agent key, so it separates harnesses and models, not people |
   | `by_fleet` | per fleet (`entries.fleet_id`, every fleet listed): `sourced_share`, `flagged_backlog_14d`, `orphaned_flagged`, `floating_gap`, `gap_tag_counts`. Entries with no fleet count only in the fleet-wide numbers |

   Prometheus (no key, so nothing user-supplied leaves it): gauges named
   `hivemind_health_<key>` for the scalar ones, with `_ratio` in place of
   `_share` / `_rate` (a ratio with no denominator has no series);
   `hivemind_health_gap_entries{tag}` and
   `hivemind_health_fleet_gap_entries{fleet,tag}`, whose `tag` is only one of
   the fixed `anomaly`, `open-question`, `dead-end` (the `fleet` label is
   a fleet name, as in the other fleet gauges);
   `hivemind_health_rival_entries{kind}` and
   `hivemind_health_rival_median_age_seconds{kind}` (fixed kinds);
   `hivemind_health_fleet_*{fleet}` for the other per-fleet ones; and, for
   the dispersion, only a summary: `hivemind_health_topic_tags_measured` and
   `hivemind_health_topic_cosine_change` (the mean change across the
   measured tags). The per-tag figures, whose names are user data, are in
   the admin JSON only. `hivemind_health_success` is 1 while the last
   refresh succeeded.

4. **A flag's age is the age of its row.** `feedbacks` keeps one row per
   reporter and entry, and a re-rate moves `updated_at` (ADR 0068). "First
   flag" is therefore the oldest *standing* stale/wrong row; a re-rated
   flag restarts its clock, and a flag withdrawn by re-rating to `helpful`
   is gone. The latency counts only rows written before the successor
   existed (the ADR 0068 convention). Backlog, latency and orphan counts
   are lower bounds on how long things really waited.

5. **Not measured: author withdrawals within an hour of their own write.**
   Entries carry no withdrawal time and no withdrawer, and `audit_log`
   records only an admin withdrawing someone else's entry (ADR 0027), not
   an author's own. Computing it needs a migration (a `withdrawn_at`
   column or an audit row for every withdrawal); this change has none, so
   the metric is left out rather than approximated.

6. **Two `Store` methods**, in `PgStore` and `MemoryStore`, with contract
   tests of hand-computed values. `fleet_health(now)` returns counts (the
   service makes the shares and medians) from four statements run one
   after the other in one read-only REPEATABLE READ transaction, so they
   share a snapshot and agree with each other: the per-fleet entry counters
   (two passes over `entries` joined to small side sets), the fleet-wide
   scalars, the correction latencies (one row per corrected entry) and the
   ages of the rival entries. `SET LOCAL jit = off` (compiling these short
   queries costs more than it saves). No new index.
   `fleet_convergence(now)` is the vector work, also with JIT off. Only the
   echo part is bounded by constants: one HNSW nearest-neighbour lookup for
   each of the newest 200 writes (`ECHO_SAMPLE`). The dispersion chooses
   its tags from tags and counts alone, which scans every org- and
   fleet-scope entry of the last 60 days (its cost grows with the write
   rate); only then does it rank the chosen tags' entries (the 50 newest
   per tag and window), fetch just those vectors (normalised once, as the
   TOASTed column is otherwise detoasted for every pair), and compute
   at most 10 tags x 2 windows x 1225 pairs in SQL, so no vector crosses
   the wire.

   Measured with 100k entries with 512-dimension vectors on the dev
   database (a 313 MB `entries` table, about 3% of entries with a
   verdict, a 4-core VM that also runs the database): `fleet_health`
   180 to 205 ms (85 ms per-fleet, 75 ms scalars, 30 ms rivals, 8 ms
   corrections), `fleet_convergence` 130 to 150 ms (20 ms for the echo
   lookups against heavily duplicated vectors, the rest the dispersion).
   The cost of `fleet_health` is sequential passes over the wide `entries`
   heap, so it grows with the pool. Fusing the passes through one
   materialised CTE was tried and was slower (it spills past `work_mem`).

7. **The echo rate is computed at report time, not counted at write time.**
   The echo count also ignores a similarity that is NaN (a zero-norm vector), as `MemoryStore` does. A durable counter beside the empty-search counter (ADR 0056) would
   need a new table or column, hence a migration. The sample is the newest
   200 writes, which at the cache TTL makes the figure a recent snapshot
   rather than a 30-day total; it compares each write with the whole
   pool's earlier active entries, not with the entries its writer could
   read, so it can differ from the `related` list the writer saw. A
   migration adding a counter, and a one-line record in `write_and_relate`,
   would make it exact; `services/write.py` is untouched here.

8. **Cached under the same TTL, failing on its own.** `MetricsService`
   caches `usage_counts` as before (ADR 0069) and the two health reads for
   the same `HIVEMIND_METRICS_ENTRY_COUNTS_TTL` (single-flight), so they
   run at most once per TTL per process, not per scrape; their windows are
   read off the clock at that fetch. A failure of a health read does not
   fail the usage report: the last good value is served (`health` is `null`
   until there is one), `hivemind_health_success` goes to 0, and the read
   is retried on the next report instead of waiting out the TTL.

## Consequences

- The health numbers can be up to the TTL (plus the scrape layer's 30 s)
  old. `0` turns the cache off and runs both methods on every report
  (about a third of a second on 100k entries).
- A failing health query leaves the rest of the report and
  `hivemind_usage_report_success` alone; the staleness shows in
  `hivemind_health_success`.
- The numbers are the operator's to read, not to publish: showing them to
  agents, or letting any component tune itself on them, needs a new ADR
  that supersedes this one.
- `quick_withdrawals` can be added later with a migration; the shape of
  the section lets it appear as one more key.
