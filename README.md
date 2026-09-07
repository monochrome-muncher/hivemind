# hivemind

Hivemind is a shared memory service for the AI agents of an organization: one Postgres-backed pool that every agent in the org reads and writes, so work done with one agent (analyst 1, agent 1, day one) is diggable work for every other agent (analyst 2, agent 2, days later).

**v1 in one paragraph.** Agents write distilled, explicit **entries** — a `fact`, an `insight` (long-form analysis), or a `decision` — each carrying full provenance (author, agent, memory date, sources) and an optional `supersedes` link. Agents retrieve via **hybrid search** (keyword + vector, RRF-fused, decay-aware) with **progressive disclosure** (compact hits first, full entry on `get`). The pool is **append-only**: corrections are explicit **supersessions**, nothing is edited or silently deleted; entries can also be **withdrawn** (own, or any by the org operator). A **client-side kill switch** lets an agent's owner turn Hivemind off for a session (spitballing, non-analyst work) without the server ever knowing. One self-hosted instance per organization: one service + one Postgres (pgvector), docker-compose, no Redis.

## Key properties (v1)

- **Explicit writes only** — the agent decides what matters; no passive transcript capture
- **Flat org pool** — everyone can read and write everything in the org (a `scope` tag is the seam for future narrowing)
- **Append-only, supersession-based** — entries are immutable; a successor always outranks what it superseded
- **Hybrid retrieval** — BM25-style keyword + pgvector dense, fused (RRF), re-scored by importance × recency × feedback quality
- **Dumb outcome feedback** — `helpful` / `stale` / `wrong` per entry nudges retrieval ranking; no learned tuning
- **Agent-facing API** — REST (canonical) + MCP tools (`hive_write`, `hive_search`, `hive_get`, `hive_list`, `hive_withdraw`, `hive_feedback`); no direct DB access, no human UI in v1

## Reading order

1. [SPEC.md](SPEC.md) — the full v1 spec: domain model, API, retrieval, deployment, non-goals, and documented extensions
2. [CONTEXT.md](CONTEXT.md) — the canonical glossary (what "entry", "supersession", "memory date", "kill switch", etc. mean)
3. [docs/adr/](docs/adr/) — the decisions and their reasons (append-only entries, flat pool, client-side kill switch, explicit writes, fixed-dimension pgvector, RRF hybrid retrieval, single-Postgres deployment, credential model)
4. [AGENTS.md](AGENTS.md) — how to work in this repo (for agents and humans)

## Status

Specification-complete, implementation not started. The repo currently contains the spec, glossary, and ADRs only.