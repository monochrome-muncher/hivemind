# Hivemind app image — ONE generic image for ALL console entrypoints.
#
# The entrypoint script (entrypoint.sh) selects the runner via the
# HIVEMIND_RUNNER env var (api | mcp-http | migrate | keys) and execs
# the matching console script, forwarding any extra args. All
# configuration (DSN, embedder endpoint, host, port, retrieval knobs)
# is supplied at runtime via HIVEMIND_* env vars — see config/.env.example
# and DEPLOY.md for the full surface.
#
#   HIVEMIND_RUNNER=api        REST surface (hivemind-api)
#   HIVEMIND_RUNNER=mcp-http   hostable multi-agent MCP runner (ADR 0010)
#   HIVEMIND_RUNNER=migrate    idempotent schema migration (ADR 0013)
#   HIVEMIND_RUNNER=keys       key-management CLI (SPEC §8.1, ADR 0012)
#
# Default (unset): mcp-http — the existing docker-compose service
# (which sets no HIVEMIND_RUNNER) keeps working unchanged.
FROM python:3.14-slim

WORKDIR /app

# Copy the package metadata + lockfile first so the install layer stays
# cached across source tweaks, then the source tree + entrypoint.
COPY pyproject.toml README.md uv.lock ./
COPY src ./src
COPY entrypoint.sh /app/entrypoint.sh

# Build + install the package (runtime deps only; dev deps stay in the
# host venv). The build backend (uv_build) is pulled into pip's
# isolated build environment automatically.
RUN pip install --no-cache-dir .

# The container binds 0.0.0.0; the bind port comes from the deployment
# (HIVEMIND_PORT), not the image.
ENV HIVEMIND_HOST=0.0.0.0

ENTRYPOINT ["/app/entrypoint.sh"]