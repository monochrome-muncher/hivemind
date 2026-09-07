# Hivemind dev workflow. All commands are thin wrappers around uv + docker compose.
.DEFAULT_GOAL := help
PG_DSN ?= postgresql://hivemind:hivemind@localhost:5432/hivemind
export HIVEMIND_DATABASE_URL ?= $(PG_DSN)

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

.PHONY: install
install: ## Install all dependencies (dev + runtime)
	uv sync

.PHONY: pg
pg: ## Start Postgres + pgvector (docker)
	docker compose up -d postgres

.PHONY: pg-down
pg-down: ## Stop Postgres
	docker compose down

.PHONY: pg-reset
pg-reset: ## Stop Postgres and wipe its data volume
	docker compose down -v

.PHONY: migrate
migrate: ## Apply database migrations
	uv run hivemind-migrate

.PHONY: api
api: ## Run the REST API on :8000
	uv run hivemind-api

.PHONY: mcp
mcp: ## Run the MCP server over stdio (wire into your agent's MCP config)
	uv run hivemind-mcp

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