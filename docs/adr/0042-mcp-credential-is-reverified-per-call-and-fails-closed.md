# The MCP credential is re-verified on every call and a missing one fails closed

ADR 0009 promised that revoking an agent's key revokes its pool access
"immediately", and SPEC §8.4 repeated it. ADR 0010 then noted, as a
*consequence* of the per-agent stdio runner (`hivemind-mcp-pg`), that its
credential is "verified once, at process start": a revoked key, a trust
demotion or a re-homed fleet changed nothing until that process restarted,
while the process kept reading and writing the pool as the old identity
(review finding MCP-1 / AUTH-7b). The docs contradicted each other, and the
behaviour contradicted the security promise.

Two related defects sat beside it. `build_server`'s `resolve_app()` fell back
to the template credential when the per-request provider returned `None`; for
`hivemind-mcp-http` that template is the legacy, non-access-controlled
`shared` identity (full read, org write), so any future path that lost the
per-request context would have become a hole instead of an error (MCP-5 /
AUTH-7a). And the MCP 401 had no `WWW-Authenticate` header and also answered
`/.well-known/*`, which MCP clients read as "start OAuth" (ONB-8).

## Decision

1. **`hivemind-mcp-pg` re-verifies its key on every tool call** through the
   same `Authenticator.verify` the HTTP runner uses (one indexed query, no
   cache), via a credential provider passed to `build_server`. The startup
   check stays as a fail-fast. This **supersedes the "verified once, at
   process start" consequence of ADR 0010**; ADR 0009's "immediately" is now
   true of both Postgres-backed runners. A key that no longer verifies makes
   the call answer `{"error": {"code": "unauthenticated"}}`.
2. **A credential provider that yields nothing fails closed**: the call
   answers `unauthenticated`; `build_server` never falls back to the app's own
   credential when a provider is set. Only the dev runner (`hivemind-mcp`),
   which passes no provider and is explicitly the single-identity,
   in-memory path (ADR 0009), uses the fixed credential. The HTTP template
   credential is additionally access-controlled at level 0.
3. **The 401 says what it is.** `WWW-Authenticate: Bearer realm="hivemind"`
   on 401, and a plain 404 for `/.well-known/oauth-*` (answered before the
   authenticator, so no pool cost), so clients know there is no OAuth.
4. **Host/Origin validation is configurable.** `HIVEMIND_MCP_ALLOWED_HOSTS`
   and `HIVEMIND_MCP_ALLOWED_ORIGINS` (comma-separated; ADR 0024: `Settings`
   fields, so they pass the unknown-variable check) build the MCP SDK's
   `TransportSecuritySettings` with protection on. **Default: unset, which
   keeps the SDK behaviour** (protection only on a loopback bind). A strict
   default would answer 421 to every existing ingress-fronted deployment,
   whose Host is the public name, so it is opt-in; the k8s ConfigMap and
   `config/.env.example` carry a commented example. Origins without hosts is
   a startup error (the check would silently stay off).
5. **`/mcp/health/database` is cached for ~2 s** (single-flight), so the
   unauthenticated deep probe (ADRs 0019, 0037) costs at most one pool
   round-trip per window however often it is hit. Garbage-key floods on `/mcp`
   are not addressed here; that is ingress rate limiting.

## Consequences

* One extra indexed query per tool call on `hivemind-mcp-pg` (the HTTP
  runner already paid it per request). An outage of the authenticator now
  surfaces on the next call rather than only at startup.
* The error envelope gains `unauthenticated`. Agents should treat it as
  "ask the admin" (revoked or replaced key), not as a transient failure.
* ADR 0010's text is unchanged; its startup-verification consequence is
  superseded by this ADR.
