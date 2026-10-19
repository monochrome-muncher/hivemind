# Hivemind dev workflow. All commands are thin wrappers around uv + docker compose.
.DEFAULT_GOAL := help
PG_DSN ?= postgresql://hivemind:hivemind@localhost:5432/hivemind
# The dev defaults below fill in only what the environment profile file
# (`.env.local`, or the file ENVIRONMENT selects; ADR 0017) leaves unset:
# exporting a default would beat the file, since real env vars win over it.
# A shell export or `make VAR=...` still wins over both.
ENV_NAME := $(strip $(shell printf '%s' '$(ENVIRONMENT)' | tr '[:upper:]' '[:lower:]'))
ENV_PROFILE := $(if $(filter production prod,$(ENV_NAME)),.env.production,$(if $(filter staging,$(ENV_NAME)),.env.staging,$(if $(filter test,$(ENV_NAME)),.env.test,.env.local)))
PROFILE_VARS := $(shell sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?(HIVEMIND_[A-Za-z0-9_]+)[[:space:]]*=.*/\2/p' $(ENV_PROFILE) 2>/dev/null)
# $(call dev_default,NAME,VALUE): export NAME ?= VALUE unless the profile sets NAME.
dev_default = $(if $(filter $(1),$(PROFILE_VARS)),,$(eval export $(1) ?= $(2)))
# Local vLLM embedding endpoint (make vllm; ADR 0005). Override per target,
# e.g. `make api HIVEMIND_EMBEDDING_ENDPOINT=https://api.openai.com/v1`.
$(call dev_default,HIVEMIND_DATABASE_URL,$(PG_DSN))
$(call dev_default,HIVEMIND_EMBEDDING_ENDPOINT,http://localhost:8001/v1)
$(call dev_default,HIVEMIND_EMBEDDING_MODEL,Qwen/Qwen3-Embedding-0.6B)
# Dev dim 512 (fast local vLLM). The code default is 1024 (ADR 0015); a
# pool provisioned at another dim fails loudly at `hivemind-migrate`.
$(call dev_default,HIVEMIND_EMBEDDING_DIM,512)
# Locally the environment is entirely ours, so a typo'd HIVEMIND_* variable
# fails startup instead of only warning (ADR 0032).
$(call dev_default,HIVEMIND_STRICT_ENV,true)
# Entity extraction (ADR 0016, SPEC §13): empty endpoint = OFF (empty
# `entities`, no LLM cost). Best-effort: a failure never blocks a write.
# Enable per target, e.g.
#   make api HIVEMIND_EXTRACTOR_ENDPOINT=http://localhost:8080/v1 \
#            HIVEMIND_EXTRACTOR_MODEL=qwen3.8-27b HIVEMIND_EXTRACTOR_API_KEY=dummy
$(call dev_default,HIVEMIND_EXTRACTOR_ENDPOINT,)
$(call dev_default,HIVEMIND_EXTRACTOR_MODEL,)
$(call dev_default,HIVEMIND_EXTRACTOR_API_KEY,)
# Per-agent credential for the Postgres-backed MCP runner (ADR 0009). Leave
# empty unless overriding; an agent's MCP config sets its own key.
$(call dev_default,HIVEMIND_MCP_KEY,)
# Bind host/port for the local REST API (`make api`, ADR 0010).
$(call dev_default,HIVEMIND_HOST,127.0.0.1)
$(call dev_default,HIVEMIND_PORT,8000)
# Port for the streamable-HTTP MCP runner: the host port of the mcp-http
# docker service (the container always binds 8088) and the port of
# `make mcp-http-dev`, so the runner never collides with `make api`.
# docker compose never reads the profile file, so the profile's value is
# passed on here.
HIVEMIND_MCP_HTTP_PORT ?= $(or $(shell sed -n -E 's/^[[:space:]]*(export[[:space:]]+)?HIVEMIND_MCP_HTTP_PORT[[:space:]]*=[[:space:]]*["'"'"']?([0-9]+).*/\2/p' $(ENV_PROFILE) 2>/dev/null | tail -n 1),8088)
export HIVEMIND_MCP_HTTP_PORT

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

.PHONY: up
up: ## Start the full local stack in docker (postgres, vllm, api :8000, admin :8089, portal :8090, mcp-http :8088; migrations are automatic, ADR 0018)
	docker compose up -d

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

.PHONY: portal
portal: ## Run the agent portal on :8081 against the local REST API (ADR 0095; start `make api` first)
	HIVEMIND_PORTAL_API_URL=$${HIVEMIND_PORTAL_API_URL:-http://127.0.0.1:8000} HIVEMIND_PORT=8081 uv run hivemind-portal

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
mcp-http-dev: ## Run the streamable-HTTP MCP server as a local process instead of docker (port $(HIVEMIND_MCP_HTTP_PORT), default 8088, like mcp-http; ADR 0010)
	HIVEMIND_PORT=$(HIVEMIND_MCP_HTTP_PORT) uv run hivemind-mcp-http

.PHONY: mcp-http-down
mcp-http-down: ## Stop the mcp-http docker service
	docker compose down mcp-http

# The standalone stack (docker-compose.standalone.yaml): a Hivemind that
# stays up while you develop. Its database (`hivemind_live` on :5433) is
# NOT the one the test suite wipes — db_guard refuses that name, so a
# test run pointed at it fails loudly instead of TRUNCATE-ing it.
STANDALONE_COMPOSE ?= docker compose -f docker-compose.standalone.yaml

.PHONY: standalone
standalone: ## Start the standalone local stack (pg :5433 db hivemind_live, vllm :8101, api :8100, admin :8189, portal :8190, mcp-http :8188; survives `make test`)
	$(STANDALONE_COMPOSE) up -d

.PHONY: standalone-down
standalone-down: ## Stop the standalone stack
	$(STANDALONE_COMPOSE) down

.PHONY: standalone-reset
standalone-reset: ## Stop the standalone stack and wipe its data volume
	$(STANDALONE_COMPOSE) down -v

.PHONY: test
test: ## Run the full test suite (unit + integration; integration needs `make pg`)
	uv run pytest

.PHONY: test-unit
test-unit: ## Run unit tests only (no Postgres needed)
	uv run pytest tests/unit

.PHONY: measure-threshold
measure-threshold: ## Measure a search similarity threshold with the configured embedder (ADR 0062; e.g. after `make vllm`; set HIVEMIND_EMBEDDING_DIM to the deployment's)
	uv run python -m tests.eval.threshold

.PHONY: measure-confirmation measure-ranking measure-versions measure-importance
measure-ranking: measure-confirmation ## Alias: measure the off-by-default ranking changes (ADR 0071 confirmation clock, ADR 0072 recency profile per kind)

measure-confirmation: ## Measure the confirmation recency clock (ADR 0071) and the per-kind recency profiles (ADR 0072) with the configured embedder (e.g. after `make vllm`; set HIVEMIND_EMBEDDING_DIM to the deployment's)
	uv run python -m tests.eval.confirmation

measure-versions: ## Measure the version-grouping cut-off with the configured embedder (ADR 0074; e.g. after `make vllm`; set HIVEMIND_EMBEDDING_DIM to the deployment's)
	uv run python -m tests.eval.versions

measure-importance: ## Measure the narrow importance factor (ADR 0088) with the configured embedder (e.g. after `make vllm`; set HIVEMIND_EMBEDDING_DIM to the deployment's; EVAL_IMPORTANCE_JSON=path also writes JSON)
	uv run python -m tests.eval.importance

.PHONY: check
check: ## Type-check + lint (mypy strict + ruff)
	uv run mypy
	uv run ruff check src tests
	uv run ruff format --check src tests

.PHONY: format
format: ## Auto-format + fix lint issues
	uv run ruff format src tests
	uv run ruff check --fix src tests