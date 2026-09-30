# Hivemind app image — ONE generic image for ALL console entrypoints.
#
# The entrypoint script (entrypoint.sh) selects the runner via the
# HIVEMIND_RUNNER env var (api | mcp-http | admin | migrate | keys) and execs
# the matching console script, forwarding any extra args. All
# configuration (DSN, embedder endpoint, host, port, retrieval knobs)
# is supplied at runtime via HIVEMIND_* env vars — see config/.env.example
# and DEPLOY.md for the full surface.
#
#   HIVEMIND_RUNNER=api        REST surface (hivemind-api)
#   HIVEMIND_RUNNER=mcp-http   hostable multi-agent MCP runner (ADR 0010)
#   HIVEMIND_RUNNER=admin      admin panel, proxies to hivemind-api (ADR 0029)
#   HIVEMIND_RUNNER=migrate    idempotent schema migration (ADR 0013)
#   HIVEMIND_RUNNER=keys       key-management CLI (SPEC §8.1, ADR 0012)
#
# Default (unset): mcp-http — the existing docker-compose service
# (which sets no HIVEMIND_RUNNER) keeps working unchanged.
# Base image pinned by digest (DEP-3; the index digest of python:3.14-slim as
# resolved on 2026-09-30). Bump it deliberately (renovate/dependabot, or
# `docker buildx imagetools inspect python:3.14-slim`) to take base-image
# security updates.
ARG PYTHON_IMAGE=python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

# --- build stage: install the LOCKED runtime dependencies (DEP-3) -------------
# `uv sync --locked` installs exactly what uv.lock records (hash-checked) and
# fails if pyproject.toml and uv.lock disagree -- the image ships the same
# dependency set CI tested, never whatever PyPI serves today. Dev deps stay out.
FROM ${PYTHON_IMAGE} AS builder
RUN pip install --no-cache-dir uv==0.12.21
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
# Dependencies first (cached across source-only changes), then the project.
COPY pyproject.toml README.md uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

# --- runtime stage -------------------------------------------------------------
FROM ${PYTHON_IMAGE}
# Non-root (DEP-4): a numeric UID so Kubernetes `runAsNonRoot` can verify it.
# Every port the runners bind is > 1024. The app writes nothing to disk
# (PYTHONDONTWRITEBYTECODE; bytecode was compiled at build time), so the
# k8s Deployments run with a read-only root filesystem and an emptyDir /tmp.
RUN groupadd --system --gid 10001 hivemind \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin hivemind
COPY --from=builder /opt/venv /opt/venv
COPY --chmod=0755 entrypoint.sh /app/entrypoint.sh
WORKDIR /app
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

# The container binds 0.0.0.0; the bind port comes from the deployment
# (HIVEMIND_PORT), not the image.
ENV HIVEMIND_HOST=0.0.0.0

USER 10001:10001
ENTRYPOINT ["/app/entrypoint.sh"]
