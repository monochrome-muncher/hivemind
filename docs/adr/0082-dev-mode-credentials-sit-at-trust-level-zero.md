# Dev-mode credentials sit at trust level 0 when the pool requires stored keys

The v2 trust ladder (ADRs 0011–0012) governs registered agents; admin keys
carry operator privilege (ADRs 0031, 0060); the org key gates registration
(ADRs 0031, 0039). The dev runner (`hivemind-mcp`) is a keyless local tool
with a fixed `dev` identity — a hand-built `Credential` that is none of those,
so `access_controlled` stays `False` and it gets admin-equivalent rights: full
visibility and `org` writes (SPEC §8.4 calls it "the only path without a
per-call credential").

Nothing keeps such a **dev-mode credential** from acting on a *real* pool. Any
script, custom runner, or future dev-plus-Postgres combination that constructs
a `Credential` directly can read the whole pool and write `org`-scope entries,
and `author` falls back to the key's placeholder `user_id` (e.g. `dev`) — a
name that is reserved, unregistered, and not the server-verified provenance
ADRs 0012/0033 promise. Evidence: a live entry in the org pool with
`author='dev'`, `agent='scout'` (self-reported, which only non-agent
credentials may do) and `scope='org'`, written by a subagent through a
dev-mode credential; the admin panel rendered it as "dev via scout".

This is the same class of hole ADR 0042 closed for the MCP runners (a legacy
template credential that would have been full access had a per-call context
been lost); it lives on the other credential-construction path — anything
that builds a `Credential` by hand.

## Decision

1. **A credential is "stored" iff it was issued by the authenticator** — an
   admin key, an org key, or an active agent key. Anything else is a
   **dev-mode credential**. The predicate lives on `Credential`
   (`ports.py`): `is_admin or is_org or agent_name is not None`.
2. **New setting `HIVEMIND_REQUIRE_STORED_KEYS` (bool, default `false`)** — a
   `Settings` field (ADR 0024), documented in `config/.env.example` together
   with the implementation. When `true`, a dev-mode credential **sits at
   trust level 0 on the data plane** (SPEC §12.2, exactly what pending agents
   get): reads return empty / not-found, writes are `permission_denied`, and
   feedback, pins and withdrawal follow readability. No new error codes. The
   admin surface is unchanged (it already requires `is_admin`).
3. **Enforced where each surface resolves its credential**, not left to the
   runners' choice of credential: the REST dependency and `build_server`
   (with or without a credential provider, so every MCP runner) apply
   `Credential.effective`, and `build_services` passes the flag to the
   feedback and pin services, which apply it again. Every surface (REST,
   both stdio MCP runners, the HTTP MCP runner) is covered. A script that
   calls the other services directly (reads, writes, search, withdrawal) is
   not, and must apply `Credential.effective` itself. (Corrected 2026-10-27: this said "at the
   service seam (`build_services`)", which only the feedback and pin
   services do.)
4. **Off by default**, so the keyless dev runner, local scripts and the test
   fakes (which construct credentials directly) are unchanged; production
   deployments (k8s ConfigMap) set it `true`.

## Consequences

* Zero behaviour change at the default; one new `HIVEMIND_*` variable that a
  `Settings` field absorbs, so `HIVEMIND_STRICT_ENV` needs no allow-list entry
  (ADRs 0024, 0032).
* With the flag on, a keyless dev credential against a real pool fails closed
  like a pending agent — 403 on writes, empty reads — so a misconfigured
  script can no longer write `org` entries under a placeholder author.
  Operators with ad-hoc scripts must use an agent key, which is the point.
* The `hivemind-mcp-http` template credential (ADR 0042) is already
  access-controlled at level 0; this closes the same class of hole for every
  other credential-construction path.
