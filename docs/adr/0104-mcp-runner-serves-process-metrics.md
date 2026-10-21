# The MCP HTTP runner serves per-process metrics

Amends [ADR 0050](0050-prometheus-scrape-endpoint.md) (its consequence that the MCP runners serve no `/metrics`, for `hivemind-mcp-http`) and adds to [ADR 0019](0019-unauthenticated-orchestrator-probe-endpoints.md)'s unauthenticated surface.

Status: Accepted. The stdio runners (`hivemind-mcp`, `hivemind-mcp-pg`) still serve nothing.

## Context

ADR 0050 left MCP traffic and MCP degraded searches uncounted, and a Prometheus scrape of `hivemind-mcp` hit the bearer-key middleware and got 401. Agents reach the pool mostly through `hivemind-mcp-http`, so an embedder outage that degrades their searches (ADR 0048) was invisible to the alert on `hivemind_search_degraded_total`, which only the API's own searches incremented.

## Decision

1. **`hivemind-mcp-http` serves `GET /metrics`** in the Prometheus text format, unauthenticated, answered by the probe router before the auth middleware (like the probes, ADR 0019), **only when `HIVEMIND_MCP_METRICS_ENABLED=true`** (`Settings.mcp_metrics_enabled`, default false). When off, `GET /metrics` is a 404 before the key check (so a misdirected scrape is not an auth failure) and nothing is collected: no `McpMetrics`, no request wrapper. It uses its own `CollectorRegistry` per app.

2. **Series, per process only:**
   - `hivemind_http_requests_total{method,route,status}` and `hivemind_http_request_duration_seconds{method,route}`: the names, labels and buckets of the REST runner's (the code is shared in `hivemind.http_metrics`). Counted outermost, so a 401 and a probe answer are counted. `route` is the exact path when it is one the runner serves (`/mcp`, the three probe paths, `/metrics`), otherwise `unmatched`, so a caller cannot grow the label set. The `method` label is bounded the same way: GET, HEAD, POST, PUT, PATCH, DELETE or OPTIONS, anything else `other`. This applies to the REST runner too.
   - `hivemind_search_degraded_total`: same name and help as the REST one, incremented when `hive_search` answers `degraded: keyword_only`. The hook is optional on `McpHivemind`; the stdio runners pass none.
   - `hivemind_mcp_tool_calls_total{tool,outcome}` and `hivemind_mcp_tool_duration_seconds{tool}`, recorded where `register_tool` binds each tool. `tool` is one of the ten registered names; `outcome` is `ok`, or `error` for an error reply (`{"error": {...}}`) or a raise (re-raised after counting).

3. **No usage series.** The usage gauges, the search and flag counters and `hivemind_health_*` stay on `hivemind-api`. They name agents and fleets, and serving them here would run a second grouped scan of the pool and put names on the MCP pod. No label on the MCP endpoint carries an agent, fleet, owner or key.

## Security trade-off

Like the API's `/metrics`, this endpoint has no key, so it is off by default and on only where the port is not public.

- **Off unless `HIVEMIND_MCP_METRICS_ENABLED=true`.** The shipped Kubernetes Deployment sets it, since its public Ingress sends only the `/mcp` prefix to the MCP Service. The compose files leave it off: they publish the MCP port on every interface while the API's `/metrics` there is bound to loopback.
- **What it reveals:** request volume and latency, the per-tool call mix and error share, the count of 401 answers (an outsider could watch the 401 count to follow a key-guessing run) and the degraded-search count. No agent, fleet, owner name, key or tool argument appears in any series.
- **Reachable some other way** (a LoadBalancer, a compose host port behind a TLS proxy): leave it off, or block `/metrics` in the proxy.

The optional NetworkPolicy admits a `monitoring` namespace on the MCP port, as for the API.

## Consequences

- Aggregate the process counters across the `api` and `mcp` jobs with `sum`: `sum(rate(hivemind_search_degraded_total[5m]))` now covers both surfaces. The usage gauges remain per pool, `max` across API replicas.
- `hivemind_http_requests_total` from the two jobs differ by `route`; filter by `job` or route to separate them.
- An older MCP image answers a scrape with 401; with the setting off the answer is 404.
- The pod annotations, the ServiceMonitor and the NetworkPolicy ship for the MCP pods.
