# Hivemind app image: ONE image for all console entrypoints. entrypoint.sh
# picks the runner from HIVEMIND_RUNNER (unset = mcp-http) and forwards
# extra args; all configuration comes from HIVEMIND_* env vars at runtime
# (config/.env.example, DEPLOY.md).
#
#   HIVEMIND_RUNNER=api        REST surface (hivemind-api)
#   HIVEMIND_RUNNER=mcp-http   hostable multi-agent MCP runner (ADR 0010)
#   HIVEMIND_RUNNER=admin      admin panel, proxies to hivemind-api (ADR 0029)
#   HIVEMIND_RUNNER=migrate    idempotent schema migration (ADR 0013)
#   HIVEMIND_RUNNER=keys       key-management CLI (SPEC §8.1, ADR 0012)
#
# Base image pinned by digest (DEP-3; python:3.14-slim as of 2026-09-30).
# Bump deliberately for security updates
# (`docker buildx imagetools inspect python:3.14-slim`).
ARG PYTHON_IMAGE=python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

# --- build stage: install the LOCKED runtime dependencies (DEP-3) -------------
# `uv sync --locked` installs exactly uv.lock (hash-checked) and fails if it
# disagrees with pyproject.toml, so the image ships what CI tested. No dev deps.
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
# Non-root (DEP-4): a numeric UID so `runAsNonRoot` can verify it; all ports
# are > 1024. Nothing is written to disk (bytecode compiled at build time),
# so k8s runs a read-only root filesystem with an emptyDir /tmp.
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
