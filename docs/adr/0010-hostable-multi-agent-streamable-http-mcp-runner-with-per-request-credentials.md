# Hostable, multi-agent streamable-HTTP MCP runner with per-request credentials (ADR 0010)

ADR 0009 ships a *per-agent* Postgres-backed MCP runner (`hivemind-mcp-pg`):
one thin process per agent, each bound to a single verified credential, all
sharing one Postgres pool. That is the right shape for a dev machine, but
it has two operational costs that only bite at scale:

* **N processes, N connection pools.** Every agent owns a process and its
  own asyncpg pool. The server must supervise N children, and Postgres
  connection count grows linearly with agent count.
* **Credential verified once, at process start.** A revoked key is only
  honoured on the agent's *next* restart — a running process keeps its
  access until it is restarted.

**Decision: add a third runner, `hivemind-mcp-http` (`main_http`), a
single long-lived streamable-HTTP process serving an unlimited number of
agents, each authenticating *per request*.** One `PgStore` + one
OpenAI-compatible embedder + one `Authenticator` pool (the same DSN /
embedder / credentials the REST API uses). The acting credential is
resolved **per request** by an ASGI auth middleware that verifies the
request's API key against the `credentials` table (ADR 0008) and stashes
the resolved `Credential` in a per-task context var; `build_server`
re-binds the shared, *stateless* services to that credential on every
tool dispatch.

Considered options:

* **(a)** Keep only the per-agent stdio runner (ADR 0009) — rejected as
  the *general* form: N processes and startup-time credential verification
  do not scale, and revocation is delayed to the next restart.
* **(b)** Make the MCP server proxy each tool to the REST API over HTTP —
  rejected (as in ADR 0009): an extra hop + a second auth layer for a
  capability already in-process.
* **(c)** A single hostable streamable-HTTP process, per-request
  credential resolution against the `Authenticator` port — **chosen**.

Consequences:

* **One process, one pool, per-request auth.** `hivemind-mcp-http` is a
  single long-lived process with one Postgres pool and one embedder. Each
  request presents its own agent-scoped key (ADR 0008); the middleware
  re-verifies it **per request** (via the `Authenticator` port), so
  `hivemind-keys revoke` takes effect on the very next request — no
  restart.
* **Shared, stateless services; per-request identity.** The `Store`,
  `Embedder`, and services are *stateless* and shared across all
  requests; only the acting `Credential` varies per request (the identity
  is re-bound on every dispatch). One process = many agents.
* **Per-request transport: stateless streamable-HTTP.** The server runs
  the SDK's stateless streamable-HTTP transport (one request = one
  self-contained exchange, no cross-request session state). The pool is
  stateless with respect to sessions, so this is a natural fit.
* **Three runners, three shapes.** The dev runner (`hivemind-mcp`,
  in-memory) and the per-agent runner (`hivemind-mcp-pg`, ADR 0009) are
  unchanged; this ADR adds the hostable runner for the "many agents, one
  machine" production case. All three read/write the same Postgres pool
  with the same verified provenance.