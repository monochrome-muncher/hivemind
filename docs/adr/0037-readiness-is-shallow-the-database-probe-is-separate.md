# Readiness is shallow; the database probe is a separate endpoint

Supersedes the "deep readiness" part of ADR 0019.

## Context

ADR 0019 made `GET /mcp/health` deep (200 only when the Postgres pool
answers `SELECT 1`, else 503) and pointed the MCP Deployment's
`readinessProbe` at it, so a database outage marked MCP pods NotReady.

In a deployment, a database outage did exactly that — to every replica at
once, because they all share the one database. The Service had no
endpoints left, so agents' `/mcp` calls got a 503 from the proxy and
concluded Hivemind was off; and while the check kept failing after the
database came back, nothing could serve even the requests that would have
worked. The check discarded its exception, so the pod log said nothing
about why.

Draining traffic from a pod only helps when *another* pod is healthy. With
one shared dependency, none is.

## Decision

1. **`GET /mcp/health` is shallow**: 200 whenever the process answers, like
   `/mcp/liveness`. It stays the readiness path, so existing probe
   configuration (the shipped manifest, an Auto DevOps chart) needs no
   change.
2. **`GET /mcp/health/database` is the deep check**, for people and
   monitoring: 200 `{"status": "ok", "database": "ok"}`, or 503
   `{"status": "unhealthy", "database": "unreachable", ...}`. It is bounded
   by a 5-second timeout, so a check stuck on the pool answers instead of
   hanging. No probe uses it.
3. **The cause is logged**: the store logs a WARNING with the exception
   when the check starts failing, and an INFO line when it passes again —
   once per outage, not per probe. The response never carries connection
   details.

## Consequences

* During a database outage every MCP pod stays in service and requests
  fail with their own errors; they succeed again the moment the database
  is back, with no readiness round-trip.
* Liveness is unchanged (`/mcp/liveness`): a database outage still never
  restarts a pod.
* Monitoring that relied on `/mcp/health` returning 503 must move to
  `/mcp/health/database`.
