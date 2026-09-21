# Unauthenticated orchestrator probe endpoints

The k8s deployment story (Tier 3.4) probed the `hivemind-mcp`
Deployment with a hack: an `exec` probe that curled `/mcp` and
treated a **401 from the auth middleware as "healthy"** (the compose
healthcheck did the same). That worked, but it is fragile — the
health semantics of the whole stack are encoded in "which error code
the auth wall returns," and k8s probes by design carry **no
credential**.

## Decision

1. **Thin, unauthenticated probe endpoints, served OUTSIDE the auth
   middleware (ADR 0019 → `ProbeRouter` in `mcp/http.py`).**
   Orchestrators (k8s liveness/readiness probes, compose healthchecks,
   load-balancer health checks) must be able to ask "is this process
   up / can it serve?" without holding an API key.
2. **Two semantics, two paths (the mcp-http runner):**
   - `GET /mcp/liveness` — **shallow**: 200 whenever the process
     answers (no dependency checks). k8s liveness (don't restart a
     process that is merely behind).
   - `GET /mcp/health` — **deep**: 200 only when the Postgres pool
     answers a liveness check (`Store.health_check` — `SELECT 1`);
     503 `{"status": "unhealthy", "detail": "database unreachable"}`
     otherwise. k8s readiness (drain traffic when a dependency is
     down, without restarting the pod).
3. **A shallow liveness on the REST API too:**
   `GET /v1/liveness` (public, unauthenticated, ADR 0019). The REST
   surface's existing `GET /v1/health` stays the **static 200 it is
   today** (SPEC §5.1: liveness is public by design, no DB check —
   v1 posture). A DB outage on the API shows as request errors, not
   NotReady.
4. **`Store` gains a `health_check()` port method** (ADR 0019 →
   `ports.py`): `MemoryStore` returns True (in-memory pool is always
   up), `PgStore` returns True on a `SELECT 1` and **False (never
   raises)** when the pool is unreachable / dropped — the 503 is the
   orchestrator-facing signal, a crash in the probe is not.
5. **Probe paths are GET-only and served BEFORE the auth middleware.**
   Non-GET methods, non-probe paths, and non-HTTP scopes (lifespan)
   are delegated untouched — the MCP transport surface (POST/GET
   `/mcp`) is unchanged and still requires a valid key.
6. **The k8s + compose probes switch to the new endpoints**
   (ADR 0019 → the accompanying manifest/manifest commit):
   mcp Deployment `livenessProbe` → `httpGet /mcp/liveness`,
   `readinessProbe` → `httpGet /mcp/health`; the compose healthcheck
   → `GET /mcp/health` (200/503). The fragile 401-exec hack is
   retired.

## Consequences

- `mcp/http.py`: a `ProbeRouter` ASGI wrapper (the outermost layer)
  + the two probe paths; `build_http_app` returns
  `ProbeRouter(BearerAuthMiddleware(mcp_app, ...), store)`.
- `api/routes.py`: `GET /v1/liveness` (public, shallow) — the
  unauthenticated twin of `/v1/health`.
- `Store.health_check()` joins the `Store` protocol (`MemoryStore`
  = always healthy, `PgStore` = pool liveness, hermetic fakes =
  True) — so a probe can check the *dependency*, not just the
  process.
- k8s `readinessProbe` on `hivemind-mcp` is now **deep** (503 on a
  DB outage = NotReady = traffic drained); the API Deployment's
  readiness stays the static `/v1/health` (SPEC §5.1 posture).

## Alternatives considered

- *Keep the 401-as-healthy exec probe (status quo)*: rejected — the
  semantics live in "which error the auth wall returns," probes
  still effectively depend on the auth code path, and a future
  auth change (e.g. 403 vs 401) would silently flip the probe.
- *A k8s-only readiness sidecar / `--health-cmd` flag on uvicorn*:
  rejected — the same endpoints are needed by the compose dev
  healthcheck and by any LB in front of the service; an endpoint is
  the least surprising place for orchestrators to look.
- *Deep probes on both runners (a `/v1/health` DB check)*: rejected
  for v1 — SPEC §5.1 commits the API's `/v1/health` to a public,
  static liveness; changing its semantics is a spec change, not an
  ops change. (Revisit if a readiness-gating requirement for the
  API surface lands.)