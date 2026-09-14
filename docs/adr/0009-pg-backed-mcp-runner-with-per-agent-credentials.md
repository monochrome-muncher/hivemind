# Postgres-backed MCP runner with per-agent credentials (ADR 0009)

The MCP stdio surface ships **two runners**.

* ``hivemind-mcp`` (``main``) is the **dev** path: an in-memory store +
  local hash embedder + a hard-coded ``dev`` credential. Zero network,
  ephemeral, single identity — for exercising the six tools with no
  dependencies.
* ``hivemind-mcp-pg`` (``main_pg``, **this ADR**) is the **production**
  path: a DSN-backed ``PgStore`` + the operator-configured
  OpenAI-compatible embedder (ADR 0005), with the acting credential
  resolved by verifying ``HIVEMIND_MCP_KEY`` against the Postgres
  ``credentials`` table (ADR 0008).

Several agents each run their own ``hivemind-mcp-pg`` process with a
distinct key, so they all read/write the **same** Postgres pool over a
**unified** MCP interface (the same six ``hive_*`` tools) while every
write carries that agent's *verified* provenance (author + agent
instance, ADR 0008).

Considered options:

* **(a)** Agents call the REST API directly — rejected *for this need*:
  the operator wanted a single **MCP** interface, not raw HTTP calls.
* **(b)** An MCP server that proxies each tool to the REST API over
  HTTP — rejected: it adds a network hop and a second auth layer for a
  capability that is already in-process. The services + ``PgStore`` are
  in the same process, so a proxy only adds latency and a failure mode.
* **(c)** A DSN-backed MCP runner that reuses the in-process ``PgStore``
  + services + the configured embedder, bound to one verified credential
  per process — **chosen**.

Consequences: the MCP runner is one thin process per agent, all sharing
the single Postgres node (ADR 0007) and the org's configured embedder
(ADR 0005). Per-agent attribution is **server-verified** (ADR 0008),
never self-reported. The kill switch is unchanged and stays client-side
(ADR 0003). Revoking an agent's key revokes its pool access immediately
(no per-agent learned state to clean up). The dev runner (``main``) is
left untouched as the zero-dependency path.