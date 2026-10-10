# Cache the entry scan behind the usage report

Amends [ADR 0050](0050-prometheus-scrape-endpoint.md).

## Context

`MetricsService.usage_report` folds every entry counter from one grouped
scan of `entries` (`Store.usage_counts`), a sequential scan and hash
aggregate: 116 ms with 200k entries. ADR 0050 cached the whole report for
30 s in the Prometheus layer, so each replica scans about every 30 s. The
admin-key `GET /v1/metrics`, which the admin panel loads on two views, had
no cache and scanned on every call. The agent, fleet and search listings
are small tables and cost next to nothing.

## Decision

1. **`MetricsService` caches the `usage_counts()` rows** for
   `HIVEMIND_METRICS_ENTRY_COUNTS_TTL` seconds (`Settings.metrics_entry_counts_ttl`,
   default 300, `>= 0`, finite). `0` turns the cache off: one scan per
   report. Both endpoints share the cache, so `GET /v1/metrics` no longer
   scans on every call.

2. **Only the scan is cached.** `list_agents`, `list_fleets` and
   `search_counts` run on every report, so the agent, fleet and search
   numbers are as fresh as before.

3. **Single-flight, failures not cached.** One scan at a time behind an
   `asyncio.Lock`; concurrent callers wait and reuse the result. A store
   error propagates and nothing is stored: the Prometheus layer still
   reports `hivemind_usage_report_success 0` and keeps its last snapshot,
   and `GET /v1/metrics` fails as it did.

4. **The Prometheus 30 s TTL stays.** It still bounds how often the cheap
   reads run and the shape of the snapshot.

## Consequences

- The entry counts (`entries`, `fleets.writes_by_fleet`, and so the
  `hivemind_entries*` and `hivemind_fleet_entries` gauges) can be up to
  the TTL old, plus the Prometheus layer's 30 s on `/metrics`. Counts the
  admin reads right after a write may not include it yet; set the TTL to
  `0` to restore a scan per report.
- The cache is per process; each replica scans at most once per TTL.
