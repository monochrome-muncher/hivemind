# hivemind

Hivemind is a shared memory service for the AI agents of an organization: one Postgres-backed pool that every agent in the org reads and writes, so work done with one agent (analyst 1, agent 1, day one) is diggable work for every other agent (analyst 2, agent 2, days later).

**v1 in one paragraph.** Agents write distilled, explicit **entries** — a `fact`, an `insight` (long-form analysis), or a `decision` — each carrying full provenance (author, agent, memory date, sources) and an optional `supersedes` link. Agents retrieve via **hybrid search** (keyword + vector, RRF-fused, decay-aware) with **progressive disclosure** (compact hits first, full entry on `get`). The pool is **append-only**: corrections are explicit **supersessions**, nothing is edited or silently deleted; entries can also be **withdrawn** (own, or any by the org operator). A **client-side kill switch** lets an agent's owner turn Hivemind off for a session (spitballing, non-analyst work) without the server ever knowing. One self-hosted instance per organization: one service + one Postgres (pgvector), docker-compose, no Redis.

## Key properties (v1)

- **Explicit writes only** — the agent decides what matters; no passive transcript capture
- **Flat org pool** — everyone can read and write everything in the org (a `scope` tag is the seam for future narrowing)
- **Append-only, supersession-based** — entries are immutable; a successor always outranks what it superseded
- **Hybrid retrieval** — keyword (Postgres FTS) + pgvector dense, fused (RRF), re-scored by importance × recency × feedback quality (a true-BM25 extension is a drop-in upgrade, not a v1 dependency — SPEC §6.2)
- **Dumb outcome feedback** — `helpful` / `stale` / `wrong` per entry nudges retrieval ranking; no learned tuning
- **Agent-facing API** — REST (canonical) + MCP tools (`hive_write`, `hive_search`, `hive_get`, `hive_list`, `hive_withdraw`, `hive_feedback`); no direct DB access, no human UI in v1

## Reading order

1. [SPEC.md](SPEC.md) — the full v1 spec: domain model, API, retrieval, deployment, non-goals, and documented extensions
2. [CONTEXT.md](CONTEXT.md) — the canonical glossary (what "entry", "supersession", "memory date", "kill switch", etc. mean)
3. [docs/adr/](docs/adr/) — the decisions and their reasons (append-only entries, flat pool, client-side kill switch, explicit writes, fixed-dimension pgvector, RRF hybrid retrieval, single-Postgres deployment, credential model)
4. [AGENTS.md](AGENTS.md) — how to work in this repo (for agents and humans)
5. [ROADMAP.md](ROADMAP.md) — what to build next, in what order (the living plan)

## Developing

The dev environment is `uv`-managed (Python 3.14); Postgres (with pgvector) runs in docker.

```bash
make install        # uv sync (creates .venv)
make pg             # start Postgres (pgvector) in docker on :5432
make vllm           # start the local vLLM embedding server (CPU docker) on :8001
make migrate        # apply idempotent DB migrations
make test-unit      # unit tests only (no Postgres needed)
make test           # full suite (integration tests skip cleanly if Postgres is down)
make check          # mypy strict + ruff
make api            # run the REST API (hivemind-api)
make mcp            # run the MCP dev server over stdio (hivemind-mcp; in-memory)
make mcp-pg         # run the Postgres-backed MCP server over stdio (hivemind-mcp-pg; ADR 0009)
make mcp-http       # run the hostable, multi-agent streamable-HTTP MCP server (hivemind-mcp-http; ADR 0010)
```

Configuration is via `HIVEMIND_*` environment variables (see `src/hivemind/config.py`):
`HIVEMIND_DATABASE_URL` (default `postgresql://hivemind:hivemind@localhost:5432/hivemind`),
`HIVEMIND_EMBEDDING_ENDPOINT` / `HIVEMIND_EMBEDDING_API_KEY` / `HIVEMIND_EMBEDDING_MODEL` /
`HIVEMIND_EMBEDDING_DIM` (the deploy-time embedding decision, ADR 0005), and the retrieval knobs
(`HIVEMIND_RRF_K`, `HIVEMIND_WEIGHT_KEYWORD`, `HIVEMIND_WEIGHT_VECTOR`, `HIVEMIND_HALF_LIFE_DAYS`, ...).

**Fully local embeddings (ADR 0005).** The `vllm` compose service runs
`Qwen/Qwen3-Embedding-0.6B` on a CPU vLLM image (`make vllm`, port 8001 —
the `HIVEMIND_EMBEDDING_ENDPOINT` default is `http://localhost:8001/v1`).
Point the API at it with 512-dim (Matryoshka) output:

```bash
make vllm
HIVEMIND_EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B \
HIVEMIND_EMBEDDING_DIM=512 \
make api
```

The API's OpenAI-compatible embedder sends the configured dimension on
every request; a provider that cannot honor it fails the request (the
dimension is a deploy-time contract).

## Multi-agent: one pool over a unified MCP interface (ADR 0009)

Several agents on the same machine share **one** Postgres pool over a
**unified** MCP interface (the same six `hive_*` tools) — not by calling
REST directly, but each running its own Postgres-backed MCP runner
(`hivemind-mcp-pg`) under its own verified credential. Each runner talks
to the same `PgStore` + the same embedder, so every agent reads/writes the
same pool while its writes carry that agent's *verified* provenance
(author + agent instance, server-filled from the key — ADR 0008).

1. Start the pool + embedder (no REST API needed for the MCP path):
   ```bash
   make pg && make vllm && make migrate
   ```
2. Issue one agent-scoped sub-key per agent (one per agent, distinct):
   ```bash
   uv run hivemind-keys issue --user alice --agent agent-a   # -> hm_...
   uv run hivemind-keys issue --user alice --agent agent-b   # -> hm_...
   uv run hivemind-keys issue --user alice --agent agent-c   # -> hm_...
   ```
3. Point each agent's MCP config at the same runner, each with its own key:
   ```jsonc
   {
     "mcpServers": {
       "hivemind": {
         "command": "uv",
         "args": ["run", "--directory", "/home/chris/Documents/hivemind", "hivemind-mcp-pg"],
         "env": {
           "HIVEMIND_DATABASE_URL": "postgresql://hivemind:hivemind@localhost:5432/hivemind",
           "HIVEMIND_EMBEDDING_ENDPOINT": "http://localhost:8001/v1",
           "HIVEMIND_EMBEDDING_MODEL": "Qwen/Qwen3-Embedding-0.6B",
           "HIVEMIND_EMBEDDING_DIM": "512",
           "HIVEMIND_MCP_KEY": "hm_…_a_key..."   // agent-b / agent-c use their own keys
         }
       }
     }
   }
   ```

All three agents now read/write the **same** pool. A write by agent-b is
attributed to `author=alice, agent=agent-b` (verified from the key, not
self-reported). Revoking an agent's key (`hivemind-keys revoke`) cuts it
off immediately. The REST API (`make api`) remains available in parallel
for non-MCP clients; it is **not** required by the MCP runners.

Architecture in one line: `domain` (pure data) → `ports` (the seams) → `services` (orchestration) →
adapters (`memstore`, `store` (Postgres), `embeddings`, `api`, `mcp`). Tests live at the seams:
hermetic unit tests (`tests/unit`, fakes in `tests/fakes.py`) and Postgres-backed integration tests
(`tests/integration`, skip cleanly when the DB is unreachable). See [AGENTS.md](AGENTS.md) for the
full architecture map and the quality bar.

## Multi-agent: hostable streamable-HTTP (one process, many agents; ADR 0010)

The per-agent runner above (`hivemind-mcp-pg`) is the right shape for a
small dev machine: one process per agent. When **many** agents share one
machine, run the **hostable** runner instead: a single long-lived
`hivemind-mcp-http` process serving an unlimited number of agents over
streamable-HTTP, each authenticating **per request** with its own
agent-scoped key (ADR 0008). One `PgStore` + one embedder + one
`Authenticator` pool (the same DSN / embedder / credentials the REST API
uses); every write carries that agent's *verified* provenance, and a
revoked key is cut off on the very next request (no restart — the
per-request credential model of ADR 0010, vs. the per-process model of
ADR 0009).

```bash
make pg && make vllm && make migrate
uv run hivemind-keys issue --user alice --agent agent-a   # -> hm_...
uv run hivemind-keys issue --user alice --agent agent-b   # -> hm_...
make mcp-http    # one process, many agents (default 127.0.0.1:8000)
```

Each agent's MCP config points at the **same** endpoint, with its own key:

```jsonc
{
  "mcpServers": {
    "hivemind": {
      "url": "http://localhost:8000/mcp",
      "headers": { "Authorization": "Bearer hm_…_key" }   // each agent: its own key
    }
  }
}
```

The hostable runner is **per-request**: every request verifies its own key
against the `credentials` table, so `hivemind-keys revoke` takes effect
immediately (no restart). Use `hivemind-mcp-pg` (per-agent) for a few
agents on one box; use `hivemind-mcp-http` (hostable) when many agents
share one machine or when you want immediate revocation.

## Status

v1 implemented: domain model, ports, retrieval math (RRF + decay-aware scoring + feedback quality),
services, in-memory reference store, Postgres store (asyncpg + pgvector), OpenAI-compatible embedder,
the FastAPI REST surface, and the MCP server (six verbs). The unit suite is hermetic; the
integration suite runs against dockerized Postgres and skips when it is down.

**What's next:** see [ROADMAP.md](ROADMAP.md) — the plan for the next increment (validate the
core retrieval pipeline, productionize, and the held §10 extensions with their trigger metrics).