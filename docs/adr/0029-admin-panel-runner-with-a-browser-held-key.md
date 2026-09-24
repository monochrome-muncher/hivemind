# The admin panel: a static UI and an allowlisted proxy, with the key held in the browser

## Context

The admin surface (SPEC §12.4) was reachable only through `curl` and
the `hivemind-keys` CLI. Operators wanted a browser front end. Its main
job is the **pending-agent queue**: which agents have registered and
are waiting for a key, grouped by the owner who asked. It also needs to
activate an agent (choose a trust level and home fleet, see the key
once), manage agents and fleets, read the audit log (ADR 0027), and
rotate the org key.

The constraints:

* It must deploy standalone and be pointed at `hivemind-api` with one
  env var. The target is an air-gapped Kubernetes cluster fed by GitLab,
  one project per runner type (ADR 0026).
* "Log in" means presenting an admin key. There is no user directory to
  log in against.
* The operator asked for the simplest workable design, and accepted a
  known weakness in exchange (decision 3).

## Decision

1. **A new runner in the same image: `hivemind-admin`**
   (`HIVEMIND_RUNNER=admin`). It is a small Starlette app that
   * serves a static page (`index.html`, one ES module, one stylesheet)
     from the package;
   * forwards `/v1/*` calls **same-origin** to `HIVEMIND_ADMIN_API_URL`,
     so `hivemind-api` needs no CORS configuration;
   * has its own `/healthz`, which does not call the API, so an API
     outage never restarts it.

   It never talks to Postgres and mounts no Secret. The entrypoint skips
   the migration pre-step for it. Its settings are
   `HIVEMIND_ADMIN_API_URL` (required) and `HIVEMIND_ADMIN_API_CA_BUNDLE`
   (optional, for an https API URL behind an internal CA). Both are
   `Settings` fields, so ADR 0024's unknown-variable check covers them.
   It runs as **1 replica with no PDB**: it is stateless and only
   operators use it.

2. **No build step, no framework.** The UI is plain JavaScript that
   renders client-side. An npm mirror exists, but a Node toolchain would
   be a second supply chain to patch for five screens. Server-side
   templates were considered and dropped once decision 3 put the key in
   the browser: the server never holds the key, so it cannot render
   authenticated pages.

3. **The admin key lives in the tab's `sessionStorage`.** The browser
   sends it as `X-API-Key` on each proxied call. It is cleared on sign-out
   and when the tab closes. This is the simplest option, and it was
   chosen over an encrypted `HttpOnly` cookie, which needs a shared
   session secret, and over server-side sessions, which break across
   replicas and restarts. The cost is that **any script running in the
   page can read the key.** Four mitigations make that script hard to
   get in and hard to phone home from:
   * a strict **Content-Security-Policy**: `default-src 'none'`,
     `script-src 'self'`, `connect-src 'self'`, no inline script or
     style, `frame-ancestors 'none'`;
   * all API data is rendered with **`textContent`, never an
     HTML-parsing sink**, so an agent name or owner alias cannot inject
     markup. A test fails the build if an HTML sink appears in the
     script;
   * the proxy forwards an **allowlist** only: the admin endpoints,
     `GET /v1/metrics` and `GET /v1/health`. Anything else, including
     the data plane and `POST /v1/agents`, is a 404 that never reaches
     the API, so the panel cannot be used as a relay;
   * `Cache-Control: no-store` on every proxied response (two of them
     carry raw keys). The proxy forwards only `X-API-Key`,
     `Content-Type` and `Accept`, and **never logs a header or a body.**

4. **The API still does all authorization.** The panel checks nothing
   itself. A non-admin key gets the API's 403 through the proxy, and
   sign-in is simply an admin-gated read (`GET /v1/admin/fleets`).

5. **Admin keys stay CLI-only.** The panel cannot issue or revoke admin
   keys, and there is no API for it. A leaked admin key can do
   everything the admin surface does, but it cannot mint more admin
   keys.

6. **No "via panel" marker in the audit log.** Panel actions are
   recorded exactly like direct API calls, as `admin_key` /
   `admin:<fingerprint>`. The fingerprint already says who acted. A
   "via" marker would be a header the API trusts on the caller's word,
   the same unverified-claim problem as CLI actors (ADR 0027).

## Consequences

* An XSS hole in the panel would leak an admin key, not just act with
  it. The CSP and the textContent rule are the entire defence. Moving
  the key into an `HttpOnly` cookie later is a contained change: the
  proxy would attach the header from the cookie, and the UI would stop
  handling the key.
* Anyone who can reach the panel's port can try keys against it, just
  as they could against the API directly. Expose it only on an internal
  ingress or via `kubectl port-forward` (DEPLOY.md §8). Rate limiting
  is still deferred for the API as a whole.
* A new admin endpoint is unreachable from the panel until it is added
  to the proxy allowlist. That is deliberate friction.
* The pending queue reads `GET /v1/admin/agents` and filters it in the
  browser. At the agent counts this cluster expects that is fine. A
  server-side `status` filter would be the next step if it stops being
  fine.
