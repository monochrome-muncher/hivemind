# Count searches that find nothing

## Context

Hivemind cannot tell what its agents look for and fail to find. The ROADMAP
Tier 5 trigger for Passive capture is "agents forget to write", measured as
"should have remembered X" reports, which nobody files. A search that comes
back empty is the closest signal the server already sees: an agent asked,
and the pool had nothing it could read.

The existing counters do not cover it. The Prometheus endpoint (ADR 0050)
lives in the REST runner only, but most agents search through the MCP
runners, several of which (`hivemind-mcp-pg`) are short-lived per-agent
processes with no endpoint to scrape. A per-process counter would miss
most traffic.

## Decision

1. **Count in the search service, store in the pool.** `SearchService`
   counts every first-page search made by a caller (one with a
   `Visibility`), and whether it found nothing, through
   `Store.record_search`. Every runner shares the service and the pool, so
   REST and MCP searches land in the same counters.

2. **Counts only, per home fleet.** Migration `0010.search-counts` adds
   `search_counts(fleet_id, searches, empty)`, one row per home fleet of
   the searching agent (`''` for callers without one). No query text and
   no agent name are stored: a query can carry anything the agent was
   working on, and the question this answers is "how often", not "what".

3. **What counts.** Only the first page (`offset` 0): paging past the end
   is not a miss. Searches without a caller (`visibility=None`, the eval
   harness) are not traffic. A degraded search (ADR 0048) that found
   nothing counts as a search but not as empty, since it says more about
   the embedder than about the pool.

4. **Best effort.** A failed count is logged and never fails the search.

5. **Read with the usage report.** `MetricsService.usage_report` gains
   `searches: {total, empty, by_fleet: {<fleet name>: {total, empty}}}`
   (totals include fleetless callers), so `GET /v1/metrics` shows it, and
   `GET /metrics` exposes `hivemind_searches_total{fleet}` and
   `hivemind_searches_empty_total{fleet}`. They are read from the pool, so
   every replica reports the same values: aggregate with `max`.

## Consequences

- Each first-page search adds one single-row upsert. At the scale ADR 0007
  and ADR 0026 plan for (a few requests per second at peak) that is
  negligible; concurrent searches from one fleet serialise on that fleet's
  row for the length of the upsert only.
- The empty-search rate per fleet becomes the measure for the Passive
  capture trigger and a cheap health signal: a fleet whose searches mostly
  come back empty is not writing what it needs.
- It cannot say what was missing. If that is ever needed, it is a separate
  decision with its own privacy answer.
- The counters are all-time totals; Prometheus derives rates. The JSON
  report has no time window.
