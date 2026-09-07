# AGENTS.md — Hivemind

Hivemind is a shared memory service for an organization's AI agents (one Postgres-backed pool all agents read and write). The repo is spec-first: docs define the system, the code implements it.

## Before doing anything here

Read in this order:

1. [README.md](README.md) — what Hivemind is (short)
2. [SPEC.md](SPEC.md) — the v1 spec; the source of truth for scope and behavior
3. [CONTEXT.md](CONTEXT.md) — the glossary; the source of truth for **terminology**
4. [docs/adr/](docs/adr/) — decisions and their reasons; read the relevant ADR before touching the area it governs

## Commands

The dev environment is `uv`-managed (Python 3.14); Postgres (with pgvector) runs in docker.

| Task | Command |
|---|---|
| Install deps (creates `.venv`) | `make install` (or `uv sync`) |
| Start Postgres (pgvector, `localhost:5432`, db `hivemind`) | `make pg` |
| Apply DB migrations (idempotent) | `make migrate` (or `uv run hivemind-migrate`) |
| Run unit tests (no Postgres needed) | `make test-unit` (or `uv run pytest tests/unit`) |
| Run the full suite (unit + integration; integration skips if Postgres is down) | `make test` (or `uv run pytest`) |
| Type-check (strict) + lint + format check | `make check` (mypy + ruff) |
| Auto-format + auto-fix | `make format` |
| Run the REST API | `make api` (or `uv run hivemind-api`) |
| Run the MCP server (stdio) | `make mcp` (or `uv run hivemind-mcp`) |
| Stop Postgres / wipe its data | `make pg-down` / `make pg-reset` |

Environment: the service reads `HIVEMIND_*` env vars (see `src/hivemind/config.py`). The dev default is `HIVEMIND_DATABASE_URL=postgresql://hivemind:hivemind@localhost:5432/hivemind`. Embedding knobs: `HIVEMIND_EMBEDDING_ENDPOINT` / `_API_KEY` / `_MODEL` / `_DIM` (ADR 0005).

## Architecture map (where things live)

- `src/hivemind/domain/` — the frozen domain (Entry, EntryDraft, filters, enums). No I/O.
- `src/hivemind/ports.py` — the SEAMS: `Store`, `Embedder`, `Authenticator` protocols + `Credential`. Code against these, never against a concrete adapter.
- `src/hivemind/retrieval/` — the pure retrieval math (RRF fusion, decay-aware scoring, feedback quality). No I/O; unit-tested against hand-computed values.
- `src/hivemind/services/` — the orchestration layer (SearchService, WriteService, GovernanceService). The deep modules: small interfaces, deep behavior.
- `src/hivemind/memstore.py` — `MemoryStore`: the in-memory reference Store (dev + unit tests).
- `src/hivemind/store/` — `PgStore` + `PgAuthenticator` (asyncpg + pgvector), migrations, and the `build_store` / `build_authenticator` factories.
- `src/hivemind/embeddings/` — `OpenAICompatEmbedder` (the `Embedder` port's production implementation) + the `build_embedder` factory.
- `src/hivemind/api/` — the FastAPI surface (REST §5.1): schemas, deps (auth), routes, main.
- `src/hivemind/mcp/` — the MCP server (six verbs, §5.2): app, server, local embedder.
- `tests/unit/` — hermetic unit tests (fakes from `tests/fakes.py`; no Postgres, no network).
- `tests/integration/` — Postgres-backed Store tests; they SKIP cleanly when the DB is unreachable.

## Rules

- **Terminology is the glossary.** Use the canonical terms from CONTEXT.md. Say "supersede", never "update" or "overwrite" an entry (entries are immutable — ADR 0001). "Memory date" means `occurred_at`, not `created_at`.
- **Implement what the spec commits to.** v1 scope is SPEC.md §1–§8. The non-goals (§9) are non-goals: no UI, no multi-tenancy, no knowledge graph, no passive capture, no Redis, no sharding. Build the §10 extensions only when their stated trigger fires.
- **Code at the seams.** New data access goes behind the `Store` port; new providers behind `Embedder`; new auth behind `Authenticator`. The services layer is the only orchestrator; the HTTP and MCP layers are thin (validation + auth + error mapping only).
- **TDD at the agreed seams.** Tests live at the seams (`tests/unit/`, `tests/integration/`), never against internals. Red → green, one behavior at a time.
- **Decisions go in ADRs.** A new permanent design choice gets a new `docs/adr/NNNN-slug.md` (next sequential number) + a matching SPEC.md section. A decision contradicting an existing ADR gets a NEW ADR that supersedes it — never a quiet edit of the old one.
- **Glossary stays current.** When a term is resolved (design, review, or implementation), update CONTEXT.md in the same change. CONTEXT.md is a glossary: no implementation details, no prose, no rationale (that's the ADR's job).
- **One source of truth per meaning.** The spec owns behavior; the ADRs own reasons; the glossary owns words. Don't restate spec behavior in the README or docs — link to it.
- **Quality bar:** `make check` (mypy strict + ruff) clean AND `make test` green, before a change is considered done.

## State of this repo

Spec + docs (`README.md`, `SPEC.md`, `CONTEXT.md`, `AGENTS.md`, `CLAUDE.md`, `docs/adr/0001`–`0008`) plus the v1 implementation: domain, ports, retrieval math, services, in-memory store, Postgres store (asyncpg + pgvector), OpenAI-compatible embedder, FastAPI REST surface, and the MCP server. The unit suite is hermetic; the integration suite runs against dockerized Postgres and skips when it is down.
