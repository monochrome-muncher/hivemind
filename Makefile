# Hivemind dev workflow. All commands are thin wrappers around uv + docker compose.
.DEFAULT_GOAL := help
PG_DSN ?= postgresql://hivemind:hivemind@localhost:5432/hivemind
# Local vLLM dev embedding endpoint (make vllm; ADR 0005 self-hosted path).
# Override on a target to point at a different embeddings endpoint, e.g.
# `make api HIVEMIND_EMBEDDING_ENDPOINT=https://api.openai.com/v1`.
export HIVEMIND_DATABASE_URL ?= $(PG_DSN)
export HIVEMIND_EMBEDDING_ENDPOINT ?= http://localhost:8001/v1
export HIVEMIND_EMBEDDING_MODEL ?= Qwen/Qwen3-Embedding-0.6B
# Dev dim: 512 (fast local vLLM embedding, ADR 0005 self-hosted path).
# The *code* default is 1024 (ADR 0015 — we never assume 1536); a pool
# provisioned at a different dim is a loud, actionable error at
# `hivemind-migrate` time (ADR 0015), never a silent no-op.
export HIVEMIND_EMBEDDING_DIM ?= 512
# Entity-extraction extractor (ADR 0016, SPEC §13): OFF by default — an
# empty endpoint means extraction is off and entries land with empty
# `entities` (zero LLM-extraction cost, the "no authenticator = dev
# mode" stance). Extraction is optional + best-effort: a failure never
# blocks a write. Enable per target, e.g.
#   make api HIVEMIND_EXTRACTOR_ENDPOINT=http://localhost:8080/v1 \
#            HIVEMIND_EXTRACTOR_MODEL=qwen3.8-27b HIVEMIND_EXTRACTOR_API_KEY=dummy
export HIVEMIND_EXTRACTOR_ENDPOINT ?=
export HIVEMIND_EXTRACTOR_MODEL ?=
export HIVEMIND_EXTRACTOR_API_KEY ?=
# Per-agent credential for the Postgres-backed MCP runner (ADR 0009). Leave
# empty unless overriding; an agent's MCP config sets its own key.
export HIVEMIND_MCP_KEY ?=
# Host/port for the hostable streamable-HTTP MCP runner (ADR 0010).
export HIVEMIND_HOST ?= 127.0.0.1
export HIVEMIND_PORT ?= 8000
# Host port for the mcp-http docker service (default/fallback 8088). The
# container always binds 8088 - only the host mapping changes.
export HIVEMIND_MCP_HTTP_PORT ?= 8088

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

.PHONY: install
install: ## Install all dependencies (dev + runtime)
	uv sync

.PHONY: pg
pg: ## Start Postgres + pgvector (docker)
	docker compose up -d postgres

.PHONY: vllm
vllm: ## Start the local vLLM embedding server (CPU docker; Qwen3-Embedding on :8001)
	docker compose up -d vllm

.PHONY: pg-down
pg-down: ## Stop Postgres
	docker compose down

.PHONY: pg-reset
pg-reset: ## Stop Postgres and wipe its data volume
	docker compose down -v

.PHONY: migrate
migrate: ## Apply outstanding migrations (ADR 0020; provisions the embedding column at $(HIVEMIND_EMBEDDING_DIM) — changing the dim needs `make pg-reset` first)
	uv run hivemind-migrate

.PHONY: rollback
rollback: ## Roll back the most recent migration (ADR 0020; refuses 0001 — that direction is `make pg-reset` or a restore)
	uv run hivemind-migrate --rollback $(or $(N),1)

.PHONY: schema-ref
schema-ref: ## Regenerate the schema.sql reference from the migrated pool (ADR 0020; generated, never applied)
	scripts/dump-schema-reference.sh

.PHONY: api
api: ## Run the REST API on :8000
	uv run hivemind-api

.PHONY: admin
admin: ## Run the admin panel on :8080 against the local REST API (ADR 0029; start `make api` first)
	HIVEMIND_ADMIN_API_URL=$${HIVEMIND_ADMIN_API_URL:-http://127.0.0.1:8000} HIVEMIND_PORT=8080 uv run hivemind-admin

.PHONY: mcp
mcp: ## Run the MCP server over stdio (wire into your agent's MCP config)
	uv run hivemind-mcp

.PHONY: mcp-pg
mcp-pg: ## Run the Postgres-backed MCP server over stdio (HIVEMIND_MCP_KEY required; ADR 0009)
	uv run hivemind-mcp-pg

.PHONY: mcp-http
mcp-http: ## Run the hostable, multi-agent streamable-HTTP MCP server as a detached docker service (host port $(HIVEMIND_MCP_HTTP_PORT), default 8088; ADR 0010)
	docker compose up -d mcp-http

.PHONY: mcp-http-dev
mcp-http-dev: ## Run the streamable-HTTP MCP server as a local process instead of docker (HIVEMIND_HOST/HIVEMIND_PORT; ADR 0010)
	uv run hivemind-mcp-http

.PHONY: mcp-http-down
mcp-http-down: ## Stop the mcp-http docker service
	docker compose down mcp-http

.PHONY: test
test: ## Run the full test suite (unit + integration; integration needs `make pg`)
	uv run pytest

.PHONY: test-unit
test-unit: ## Run unit tests only (no Postgres needed)
	uv run pytest tests/unit

.PHONY: check
check: ## Type-check + lint (mypy strict + ruff)
	uv run mypy
	uv run ruff check src tests
	uv run ruff format --check src tests

.PHONY: format
format: ## Auto-format + fix lint issues
	uv run ruff format src tests
	uv run ruff check --fix src tests