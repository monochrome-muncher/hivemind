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
make migrate        # apply idempotent DB migrations
make test-unit      # unit tests only (no Postgres needed)
make test           # full suite (integration tests skip cleanly if Postgres is down)
make check          # mypy strict + ruff
make api            # run the REST API (hivemind-api)
make mcp            # run the MCP stdio server (hivemind-mcp)
```

Configuration is via `HIVEMIND_*` environment variables (see `src/hivemind/config.py`):
`HIVEMIND_DATABASE_URL` (default `postgresql://hivemind:hivemind@localhost:5432/hivemind`),
`HIVEMIND_EMBEDDING_ENDPOINT` / `HIVEMIND_EMBEDDING_API_KEY` / `HIVEMIND_EMBEDDING_MODEL` /
`HIVEMIND_EMBEDDING_DIM` (the deploy-time embedding decision, ADR 0005), and the retrieval knobs
(`HIVEMIND_RRF_K`, `HIVEMIND_WEIGHT_KEYWORD`, `HIVEMIND_WEIGHT_VECTOR`, `HIVEMIND_HALF_LIFE_DAYS`, ...).

Architecture in one line: `domain` (pure data) → `ports` (the seams) → `services` (orchestration) →
adapters (`memstore`, `store` (Postgres), `embeddings`, `api`, `mcp`). Tests live at the seams:
hermetic unit tests (`tests/unit`, fakes in `tests/fakes.py`) and Postgres-backed integration tests
(`tests/integration`, skip cleanly when the DB is unreachable). See [AGENTS.md](AGENTS.md) for the
full architecture map and the quality bar.

## Status

v1 implemented: domain model, ports, retrieval math (RRF + decay-aware scoring + feedback quality),
services, in-memory reference store, Postgres store (asyncpg + pgvector), OpenAI-compatible embedder,
the FastAPI REST surface, and the MCP server (six verbs). The unit suite is hermetic; the
integration suite runs against dockerized Postgres and skips when it is down.

**What's next:** see [ROADMAP.md](ROADMAP.md) — the plan for the next increment (validate the
core retrieval pipeline, productionize, and the held §10 extensions with their trigger metrics).