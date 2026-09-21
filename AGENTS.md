# AGENTS.md — Hivemind

Hivemind is a shared memory service for an organization's AI agents (one Postgres-backed pool all agents read and write). The repo is spec-first: docs define the system, the code implements it.

## Before doing anything here

Read in this order:

1. [README.md](README.md) — what Hivemind is (short)
2. [SPEC.md](SPEC.md) — the v1 spec; the source of truth for scope and behavior
3. [CONTEXT.md](CONTEXT.md) — the glossary; the source of truth for **terminology**
4. [docs/adr/](docs/adr/) — decisions and their reasons; read the relevant ADR before touching the area it governs
5. [ROADMAP.md](ROADMAP.md) — what to build next, in what order (the living plan; see the §10 trigger-metric definitions)

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
| Run the Postgres-backed MCP runner (per-agent, stdio; ADR 0009) | `make mcp-pg` (or `uv run hivemind-mcp-pg`) |
| Run the hostable streamable-HTTP MCP runner (detached docker service, host port 8088; ADR 0010) | `make mcp-http` (or `docker compose up -d mcp-http`) |
| Stop the mcp-http docker service / run it as a local process | `make mcp-http-down` / `make mcp-http-dev` |
| Stop Postgres / wipe its data | `make pg-down` / `make pg-reset` |

Environment: the service reads `HIVEMIND_*` env vars (see `src/hivemind/config.py`). The dev default is `HIVEMIND_DATABASE_URL=postgresql://hivemind:hivemind@localhost:5432/hivemind`. Embedding knobs: `HIVEMIND_EMBEDDING_ENDPOINT` / `_API_KEY` / `_MODEL` / `_DIM` (ADR 0005).

## Architecture map (where things live)

- `src/hivemind/domain/` — the frozen domain (Entry, EntryDraft, filters, enums) + the access model (`access.py`: TrustLevel, Agent, Fleet, Visibility + the single source-of-truth visibility matrix, ADR 0011). No I/O.
- `src/hivemind/ports.py` — the SEAMS: `Store`, `Embedder`, `Authenticator` protocols + `Credential`. Code against these, never against a concrete adapter.
- `src/hivemind/retrieval/` — the pure retrieval math (RRF fusion, decay-aware scoring, feedback quality) + `eval.py` (the retrieval-quality metrics: hit@k, MRR, nDCG, ROADMAP §1.1). No I/O; unit-tested against hand-computed values.
- `src/hivemind/services/` — the orchestration layer, the deep modules (small interfaces, deep behavior). `search.py` = `SearchService` (hybrid retrieval, SPEC §6); `governance.py` = `WriteService` + `GovernanceService` (write, withdraw, feedback, SPEC §4.1/§4.2); `chain.py` = `supersession_chain` (the shared `?history` walk, SPEC §5.1/§5.2); `access.py` = `AccessService` (registration + fleet/trust management + `resolve_write_scope`, ADRs 0011–0012); `metrics.py` = `MetricsService` (the usage counters, ROADMAP §3.3).
- `src/hivemind/memstore.py` — `MemoryStore`: the in-memory reference Store (dev + unit tests).
- `src/hivemind/store/` — `PgStore` + `PgAuthenticator` (asyncpg + pgvector), migrations, and the `build_store` / `build_authenticator` factories.
- `src/hivemind/embeddings/` — `OpenAICompatEmbedder` (the `Embedder` port's production implementation) + the `build_embedder` factory.
- `src/hivemind/api/` — the FastAPI surface (REST §5.1): schemas, deps (auth), routes, main.
- `src/hivemind/mcp/` — the MCP server (seven verbs, §5.2): app, server (stdio dev + per-agent `hivemind-mcp-pg` runners), http (hostable streamable-HTTP runner, ADR 0010), local embedder.
- `tests/unit/` — hermetic unit tests (fakes from `tests/fakes.py`; no Postgres, no network).
- `tests/eval/` — the retrieval eval harness (ROADMAP §1.1): a committed golden set + a CI gate pinning a floor on hit@k / MRR / nDCG, so retrieval regressions are measured, not vibes.
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

Spec + docs (`README.md`, `SPEC.md`, `CONTEXT.md`, `AGENTS.md`, `CLAUDE.md`, `ROADMAP.md`, `DEPLOY.md`, `config/`, `deploy/kubernetes/`, `.gitlab-ci.yml`, `docs/adr/0001`–`0019`, `docs/ops-runbook.md`, `docs/dogfooding-notes.md`) plus the v1 implementation: domain, ports, retrieval math (RRF fusion, decay scoring, feedback quality + the retrieval-quality metrics in `retrieval/eval.py`), services, in-memory store, Postgres store (asyncpg + pgvector), OpenAI-compatible embedder (with bounded retries on transient failures, ADR 0014), FastAPI REST surface, and the MCP surface (three runners: stdio dev, per-agent Postgres `hivemind-mcp-pg`, and the hostable streamable-HTTP `hivemind-mcp-http` — shipped as a detached docker-compose service, host port 8088). The default embedding dimension is 1024 (ADR 0015; the dev Makefile pins 512 for fast local vLLM embedding), and `migrate` fails loudly with an actionable error when the pool's dim differs from the configured dim (ADR 0015) — never a silent no-op that later surfaces as a confusing `DataError`. The access-control & fleet model (ADRs 0011–0012, SPEC §12) is implemented end to end: domain (`domain/access.py`: TrustLevel / Agent / Fleet / Visibility + the visibility matrix), the `Store` seam (fleet / agent methods + visibility-aware reads), the v2 key model (org / agent / admin; `user` keys retired) + `AccessService` (register / activate / trust-level / home-fleet / revoke / org-key rotation), the REST surface (`POST /v1/agents` + admin endpoints), the MCP surface (`hive_register` + write-scope + visibility), and the reduced `hivemind-keys` CLI. The retrieval eval harness (`tests/eval/` + `retrieval/eval.py`) reports hit@k / MRR / nDCG on a committed golden set with a CI gate pinning a floor on them. The minimal usage-counters surface (Tier 3.3): `MetricsService` + `GET /v1/metrics` (entries / fleets / agents counters). The forward-migration path (ADR 0013, Tier 3.2): the idempotent `schema.sql` re-apply + `schema_migrations` version tracking (`current_schema_version`). The ops story (Tier 3.1): `docs/ops-runbook.md` (deployment, backups, health, key rotation). The end-to-end dogfood (Tier 1.2) was run with a real agent + real embedder (`docs/dogfooding-notes.md`); it caught one real defect — `hive_write`'s forced `scope="org"` default (in the registered MCP wrapper and the REST schema) rejected every L2/L1 omitted-scope write, bypassing ADR 0011's omitted-scope rule — now fixed at all three seams (app, registered wrapper, REST) and locked by tests, along with empty-`entry_id` guards and `fleet_id` in entry reads. Entity extraction (ADR 0016, SPEC §13 — a pre-staged §10 extension on scale ambition, trigger NOT fired) is implemented end to end: the `Extractor` port (sibling of `Embedder`) + `OpenAICompatExtractor` (`src/hivemind/extractor.py` — ADR 0014 retry pattern, all-or-nothing schema-validated `{name, kind}` facets, `build_extractor` factory; **unset `HIVEMIND_EXTRACTOR_ENDPOINT` = off**, zero LLM cost), schema v6 (`entities jsonb` + `entity_names text[]` GIN + `entities_model`, symmetric with the embedding pair), a best-effort `WriteService` hook (an extraction failure never blocks a write), and the read surface (REST `entities` filter — AND, case-insensitive — + `EntryOut` facets, MCP `hive_search` / `hive_list` + the registered tool wrappers); hermetic tests + a live E2E test (skip-unreachable; dev endpoint `http://localhost:8080/v1` / `qwen3.8-27b`). Graph-expanded retrieval + the canonical entity registry stay trigger-held (Tier 5). The production-deployment story (Kubernetes + GitLab CI/CD) is shipped: a generic single image (runner selected by `HIVEMIND_RUNNER` — api / mcp-http / migrate / keys), a kustomize manifest tree (`deploy/kubernetes/` — ConfigMap + Secret template, two 1-replica Deployments (migration is owned by the image entrypoint, ADR 0018), optional nginx/cert-manager Ingress with SSE tuning), a GitLab pipeline (`.gitlab-ci.yml` — test on pgvector, docker build/push, deploy via the pre-configured GitLab Kubernetes agent + an idempotent first-run key bootstrap that lands the admin/org keys in the k8s Secret), environment profile files (`config/.env.example` + `ENVIRONMENT` selection, ADR 0017), and the `hivemind-keys revoke-admin` subcommand that completes the admin-key rotation story (DEPLOY.md §5). Unauthenticated orchestrator probe endpoints (ADR 0019): `/mcp/liveness` + `/mcp/health` (deep DB check) on the mcp-http runner and `/v1/liveness` (shallow) on the REST API, with the static `/v1/health` (SPEC §5.1) unchanged. The unit suite is hermetic; the integration suite runs against dockerized Postgres and skips when it is down.

**What's next:** Tier 2 (access control), Tier 1 (the eval harness), **4.3** (the entity-extraction facets — ADR 0016, SPEC §13), the Kubernetes + GitLab CI/CD deployment story (Tier 3.4: `DEPLOY.md` + `deploy/kubernetes/` + `.gitlab-ci.yml` + the `config/` env-profile quickstart + `revoke-admin`), environment profiles (ADR 0017), entrypoint-owned migration (ADR 0018), and the probe endpoints (ADR 0019) are shipped; the next workstream is **Tier 4** — close the *remaining* SPEC §11 open items: **4.1, the BM25-vs-FTS decision** (run the §1.1 harness, adopt BM25 with a new ADR only if the numbers say so) and **4.2, the embedding-prefix tuning** (the 512-token default against real long-form entries). Tier 5 (the remaining SPEC §10 extensions — the graph half of the knowledge-graph row, registry, multi-hop, etc.) stays held until its triggers fire.
