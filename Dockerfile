# Hivemind app image for the hostable streamable-HTTP MCP runner (ADR 0010).
#
# Installs the `hivemind` package (which pulls its runtime deps: FastAPI +
# uvicorn, asyncpg + pgvector, the mcp SDK, httpx) and exposes the six
# `hive_*` tools via the `hivemind-mcp-http` console script. All
# configuration (DSN, embedder endpoint, host, port) is supplied by the
# compose service (see docker-compose.yaml) via HIVEMIND_* env vars.
FROM python:3.14-slim

WORKDIR /app

# Copy the package metadata + lockfile first so the install layer stays
# cached across source tweaks, then the source tree.
COPY pyproject.toml README.md uv.lock ./
COPY src ./src

# Build + install the package (runtime deps only; dev deps stay in the host
# venv). The build backend (uv_build) is pulled into pip's isolated build
# environment automatically.
RUN pip install --no-cache-dir .

# The container always binds 0.0.0.0:8088; the HOST port is chosen by the
# compose port mapping (docker-compose.yaml, default 8088).
ENV HIVEMIND_HOST=0.0.0.0 \
    HIVEMIND_PORT=8088

ENTRYPOINT ["hivemind-mcp-http"]