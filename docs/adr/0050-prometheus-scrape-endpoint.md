# The REST runner serves a Prometheus scrape endpoint

> **Status: amended by [ADR 0069](0069-cache-the-entry-scan-behind-the-usage-report.md):**
> the grouped entry scan is also cached inside `MetricsService`, so the entry gauges can lag by more than 30 s.

> **Amended by [ADR 0104](0104-mcp-runner-serves-process-metrics.md):**
> `hivemind-mcp-http` now serves its own per-process `/metrics`, so the
> MCP consequence below no longer holds for it (the stdio runners still
> serve none).

Adds to ROADMAP §3.3 (usage counters). `GET /v1/metrics` is unchanged.

## Context

Operators monitor Hivemind with Prometheus, which scrapes `GET /metrics`
in its text format without a credential. Hivemind had no such endpoint:
a scrape got 404. `GET /v1/metrics` returns the usage counters as JSON
and only to an admin key, which Prometheus should not hold. The admin
panel reads `/v1/metrics`, so it stays.

## Decision

1. **`hivemind-api` serves `GET /metrics`** in the Prometheus text format
   (`prometheus_client`), unauthenticated, outside the `/v1` prefix.

2. **Series:**
   - the usage counters of `MetricsService.usage_report` as gauges
     (`hivemind_entries{state}`, `hivemind_entries_by_scope`, `_by_kind`,
     `_by_importance_source`, `_by_author_kind`, `hivemind_fleets`,
     `hivemind_fleet_entries{fleet}`, `hivemind_agents{status}`,
     `hivemind_agents_by_trust_level`), plus
     `hivemind_usage_report_success`;
   - this process's `hivemind_http_requests_total{method,route,status}`
     and `hivemind_http_request_duration_seconds{method,route}`, labelled
     by route template (`/v1/entries/{entry_id}`; `unmatched` for a 404),
     so a caller cannot grow the label set;
   - `hivemind_search_degraded_total`: searches answered by the keyword
     stream alone (ADR 0048), which otherwise only show as a log line.

3. **The usage report is cached for 30 s**, refreshed by at most one scrape
   at a time. It is one grouped scan of the pool, too much for every scrape
   of every replica. A failed refresh keeps the last snapshot and sets
   `hivemind_usage_report_success` to 0; the scrape still answers 200, so
   the request metrics keep flowing.

4. **`/metrics` is never public.** It names agents and fleets. The optional
   Ingress routes it to the source-allowlisted `hivemind-admin-api`
   Ingress (deny by default); Prometheus scrapes the pods directly, found
   by the `prometheus.io/*` pod annotations. The optional NetworkPolicy
   admits a `monitoring` namespace on the API port.

## Consequences

- Every replica reports the same usage gauges; aggregate them with `max`,
  not `sum`. The request metrics are per process; `sum` them.
- The MCP runners (`hivemind-mcp-http`, `hivemind-mcp-pg`) serve no
  `/metrics` yet, so MCP traffic and MCP degraded searches are not
  counted.
- One new runtime dependency, `prometheus-client`.
