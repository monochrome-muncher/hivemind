# AGENTS.md — Hivemind

Hivemind is a shared memory service for an organization's AI agents (one Postgres-backed pool all agents read and write). The repo is spec-first: docs define the system, the code implements it.

## Before doing anything here

Read in this order:

1. [README.md](README.md) — what Hivemind is (short)
2. [SPEC.md](SPEC.md) — the v1 spec; the source of truth for scope and behavior
3. [CONTEXT.md](CONTEXT.md) — the glossary; the source of truth for **terminology**
4. [docs/adr/](docs/adr/) — decisions and their reasons; read the relevant ADR before touching the area it governs (`docs/adr/README.md` is the index with each ADR's status)
5. [ROADMAP.md](ROADMAP.md) — what is open and what is held (the living plan, with the trigger metrics for the SPEC §10 extensions)

## Commands

The dev environment is `uv`-managed (Python 3.14); Postgres (with pgvector) runs in docker.

| Task | Command |
|---|---|
| Install deps (creates `.venv`) | `make install` (or `uv sync`) |
| Start Postgres (pgvector, `localhost:5432`, db `hivemind`) | `make pg` |
| Apply DB migrations (ordered chain, advisory-locked; ADR 0020) | `make migrate` (or `uv run hivemind-migrate`) |
| Roll back the last migration (refuses `0001`; ADR 0020) | `make rollback` (or `make rollback N=2`) |
| Regenerate the `schema.sql` reference (generated, never applied) | `make schema-ref` |
| Check expand-and-contract against previous releases (ADR 0020) | `scripts/check-backward-compat.sh` |
| Run unit tests (no Postgres needed) | `make test-unit` (or `uv run pytest tests/unit`) |
| Run the full suite (unit + integration; integration skips if Postgres is down) | `make test` (or `uv run pytest`). **Destructive:** the integration tests `TRUNCATE` and `DROP SCHEMA` in the database named by `HIVEMIND_DATABASE_URL`, so never export a real DSN. `tests/db_guard.py` refuses non-local or non-dev database names unless `HM_TEST_ALLOW_ANY_DB=1` (not `HIVEMIND_`-prefixed, so `HIVEMIND_STRICT_ENV` does not trip). `HM_TEST_REQUIRE_PG=1` turns the Postgres skips into failures (CI sets it). `scripts/check-backward-compat.sh` is equally destructive |
| Type-check (strict) + lint + format check | `make check` (mypy + ruff) |
| Auto-format + auto-fix | `make format` |
| Run the REST API | `make api` (or `uv run hivemind-api`) |
| Run the MCP server (stdio) | `make mcp` (or `uv run hivemind-mcp`) |
| Run the Postgres-backed MCP runner (per-agent, stdio; ADR 0009) | `make mcp-pg` (or `uv run hivemind-mcp-pg`) |
| Run the hostable streamable-HTTP MCP runner (detached docker service, host port 8088; ADR 0010) | `make mcp-http` (or `docker compose up -d mcp-http`) |
| Stop the mcp-http docker service / run it as a local process | `make mcp-http-down` / `make mcp-http-dev` |
| Stop Postgres / wipe its data | `make pg-down` / `make pg-reset` |

Environment: the service reads `HIVEMIND_*` env vars (see `src/hivemind/config.py`; `config/.env.example` documents them). A new variable must be a `Settings` field or be listed in `_ALLOWED_EXTRA_ENV_VARS`: unknown `HIVEMIND_*` names warn at startup and fail under `HIVEMIND_STRICT_ENV=true`, which the dev Makefile sets (ADRs 0024, 0032). The dev default is `HIVEMIND_DATABASE_URL=postgresql://hivemind:hivemind@localhost:5432/hivemind`. Embedding knobs: `HIVEMIND_EMBEDDING_ENDPOINT` / `_API_KEY` / `_MODEL` / `_DIM` (ADR 0005).

## Architecture map (where things live)

- `src/hivemind/domain/`: the frozen domain (Entry, EntryDraft, filters, enums, pins, feedback), the access model (`access.py`: TrustLevel, Agent, Fleet, Visibility and the single visibility matrix, ADR 0011; `may_supersede`, ADR 0033) and the shared input bounds (`validation.py`, ADR 0040). No I/O.
- `src/hivemind/ports.py`: the seams, `Store`, `Embedder`, `Authenticator` (+ `Credential`), and `Extractor` lives beside them in `extractor.py`. Code against these, never against a concrete adapter.
- `src/hivemind/retrieval/`: pure retrieval math (RRF fusion, decay-aware scoring with the recency floor, feedback quality) and `eval.py` (hit@k, MRR, nDCG). No I/O; unit-tested against hand-computed values.
- `src/hivemind/services/`: the only orchestration layer. `search.py` `SearchService` (hybrid retrieval, SPEC §6; keyword-only fallback, ADR 0048); `governance.py` `WriteService` + `GovernanceService` (write, related entries, see-also links, withdraw, feedback and its readable summary, pins); `chain.py` (the `?history` walk and `get_visible_entry`); `access.py` `AccessService` (registration, fleets, trust, `resolve_write_scope`); `drafts.py` `entry_draft` (author, scope and importance provenance of a new entry, shared by `POST /v1/entries` and `hive_write`); `audit.py` (the audit-log writer, ADR 0027); `metrics.py` `MetricsService` (usage counters, served as JSON at `GET /v1/metrics` and as Prometheus gauges by `api/prometheus.py` at `GET /metrics`, ADR 0050).
- `src/hivemind/memstore.py`: `MemoryStore`, the in-memory reference Store (dev + unit tests). It must behave like `PgStore`.
- `src/hivemind/store/`: `PgStore` + `PgAuthenticator` (asyncpg + pgvector), the `build_store` / `build_authenticator` factories, `keys.py` (the `hivemind-keys` CLI), `kube_secret.py` (the first-run key bootstrap, ADR 0044). `migrations/` is the ordered chain and the schema's **source of truth** (ADR 0020); `migrate.py` applies it under a Postgres advisory lock; `schema.sql` is generated (`make schema-ref`), never applied or hand-edited.
- `src/hivemind/embeddings/` + `providers.py`: `OpenAICompatEmbedder` (bounded, jittered retries under an overall deadline, ADRs 0014, 0041) and the provider-error sanitiser (no key or URL ever reaches an error or a log).
- `src/hivemind/api/`: the FastAPI surface (SPEC §5.1): schemas, deps (auth), guards (body cap, 401-before-parse), routes, main, prometheus.
- `src/hivemind/mcp/`: the MCP server (ten tools, SPEC §5.2): `app.py` (tools and server `instructions`), `server.py` (stdio runners `hivemind-mcp` and `hivemind-mcp-pg`), `http.py` (the hostable `hivemind-mcp-http`, ADR 0010), local hash embedder.
- `src/hivemind/admin/`: the admin panel (`hivemind-admin`, ADR 0029): a static UI and an allowlisted same-origin proxy to the REST API.
- `plugins/hivemind/`: the agent plugin for Claude Code, Codex, DeepSeek Harness, Hermes, Pi, Oh My Pi and OpenCode, with setup notes for Gemini CLI. Its [README](plugins/hivemind/README.md) is for users; [DEVELOPING.md](plugins/hivemind/DEVELOPING.md) describes every part, the per-harness wiring and incognito. The repository-root `package.json` (same name as `plugins/hivemind/package.json`) exists so DeepSeek Harness, Pi and OpenCode can install it from the Git URL.
- `tests/unit/`: hermetic unit tests (fakes in `tests/fakes.py`; no Postgres, no network). `tests/integration/`: Postgres-backed tests that skip when the database is down. `tests/eval/`: the retrieval eval (see Invariants).
- Deployment: `Dockerfile` + `entrypoint.sh` (one image, runner chosen by `HIVEMIND_RUNNER`; the entrypoint runs migrations, ADR 0018), `deploy/kubernetes/` (kustomize; two replicas + a PDB per app-tier runner, ADR 0026), `.gitlab-ci.yml`, `config/` (environment profiles, ADR 0017). [DEPLOY.md](DEPLOY.md) and [docs/ops-runbook.md](docs/ops-runbook.md) are the operator docs.

## Rules

- **Terminology is the glossary.** Use the canonical terms from CONTEXT.md. Say "supersede", never "update" or "overwrite" an entry (entries are immutable — ADR 0001). "Memory date" means `occurred_at`, not `created_at`.
- **Implement what the spec commits to.** The non-goals (SPEC §9) are non-goals: no memory UI, no multi-tenancy, no knowledge graph, no passive capture, no Redis, no sharding. Build a SPEC §10 extension only when its trigger fires (ROADMAP Tier 5).
- **Code at the seams.** New data access goes behind the `Store` port (and into both `PgStore` and `MemoryStore`); new providers behind `Embedder`; new auth behind `Authenticator`. The services layer is the only orchestrator; the HTTP and MCP layers are thin (validation + auth + error mapping only).
- **TDD at the agreed seams.** Tests live at the seams (`tests/unit/`, `tests/integration/`), never against internals. Red → green, one behavior at a time.
- **Decisions go in ADRs.** A new permanent design choice gets a new `docs/adr/NNNN-slug.md` (next free number; `0038` is unused), a row in `docs/adr/README.md`, and the matching SPEC.md change. A decision contradicting an existing ADR gets a NEW ADR that supersedes or amends it, plus a status note at the top of the old one — never a quiet edit of the old decision.
- **Glossary stays current.** When a term is resolved, update CONTEXT.md in the same change. CONTEXT.md is a glossary: no implementation details, no prose, no rationale (that's the ADR's job).
- **One source of truth per meaning.** The spec owns behavior; the ADRs own reasons; the glossary owns words. Don't restate spec behavior in the README or other docs — link to it.
- **Entries are data, never instructions** (ADR 0043). The server `instructions` and the `hivemind` skill say so, and tests pin it.
- **Quality bar:** `make check` (mypy strict + ruff) clean AND `make test` green, before a change is considered done. GitHub Actions runs both on every PR (`.github/workflows/tests.yml`, against a pgvector service with `HM_TEST_REQUIRE_PG=1`) plus a Docker build.

## Invariants that are easy to break

- **Migrations.** A shipped migration is frozen (`tests/unit/migration_checksums.txt`); a correction is a new migration with a `.rollback.sql`. Schema changes are expand-and-contract (SPEC §8.6, checked by `scripts/check-backward-compat.sh`). Index builds use `CREATE INDEX CONCURRENTLY` under `-- transactional: false`, which is why `migrate` polls `pg_try_advisory_lock` instead of blocking (ADR 0025). A new migration also needs its checksum added and the expected head and counts in `tests/integration/test_migration.py` updated and `schema.sql` regenerated (`make schema-ref` against a pool migrated at dim 1024).
- **Query-time settings are not schema.** The HNSW settings (`hnsw.iterative_scan`, `hnsw.ef_search`) are `SET LOCAL` inside the search transaction, because a transaction-mode PgBouncer discards session state (SPEC §6.2). `statement_timeout` is opt-in for the same reason.
- **Retrieval eval.** `tests/eval/` holds the golden set and the CI gate that pins a floor on hit@k / MRR / nDCG; never re-pin the gate to make a change pass. The age-varied fixture and the decay / `rrf_k` experiments pin direction and arithmetic, not absolute values. The recency floor (0.8, ADR 0022) is a band, not a slider: do not tune it up. BM25 vs. Postgres FTS is deferred with its reasons in ROADMAP §4.1; don't re-derive it.
- **Approximate vector search.** The HNSW index (ADR 0025) did not reach exact-scan recall in the Postgres measurement, and nothing was tuned to hide it. Re-measure on a real corpus before changing it.
- **Keys and logs.** No raw key in any audit row, log or error; provider errors are sanitised through `providers.py`. The admin, org and agent keys are distinct kinds, one key per request (ADRs 0012, 0031, 0039).
- **Agent plugin.** `tests/unit/test_agent_plugin.py` keeps the plugin in step with the server: tool names, `hive_whoami` fields, the reminder and incognito texts, versions, the per-harness setup references, the root `package.json` `files`, and the DeepSeek Harness resolver copies in `cordis.patch.yml` (kept identical; they ignore a project `.env` that names any `HIVEMIND_*` variable). The always-on skill has a 10 KB size cap. The root `package.json` deliberately has no `peerDependencies`.
- **Releases.** A version bump touches nine files (`pyproject.toml`, `uv.lock`, `src/hivemind/__init__.py`, both `package.json`, `plugins/hivemind/plugin.yaml`, the Claude and Codex `plugin.json`, `.claude-plugin/marketplace.json`) and adds a row to the release table in `plugins/hivemind/CHANGELOG.md`; the drift test enforces both. Pushing a `vX.Y.Z` tag publishes the image.

## State of this repo

The v1 core (SPEC §1–§8), the fleet and trust model (§12), entity extraction (§13, off unless `HIVEMIND_EXTRACTOR_ENDPOINT` is set), the Kubernetes deployment, the admin panel and the agent plugin are all implemented. The current release and what changed for agents in it are in `plugins/hivemind/CHANGELOG.md`; what each feature does is in SPEC.md, and why in the ADRs. ROADMAP.md lists what is open: the follow-ups in §3.13, the open half of §4.2, the re-measure triggers in §4.6, and the held §10 extensions. Local dev endpoints used by the live tests: the embedder at `http://localhost:8001/v1` (`make vllm`) and an optional extractor at `http://localhost:8080/v1`.
