#!/bin/bash
# SessionStart hook for Claude Code on the web: gives a cloud session the
# same toolchain as .github/workflows/tests.yml so `make check` and the full
# `uv run pytest` (unit + Postgres integration) work without manual setup.
#
# Python: the container's stock uv (0.8.x) resolves "3.14" to a release
# candidate that breaks pydantic, so install the pinned uv and Python first.
# Postgres: there is no docker daemon, so run the container's Postgres 16
# directly with pgvector built from source (Ubuntu ships 0.6; migrate needs
# >= 0.8). The Postgres stage is best-effort: if it fails, the session still
# starts with Python ready and the integration tests skip.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Keep in step with .github/workflows/tests.yml.
UV_VERSION="0.12.21"
PYTHON_VERSION="3.14.7"
PGVECTOR_VERSION="0.8.6"
PG_MAJOR="16"
PG_DATA="/var/lib/postgresql/hivemind-data"
# A throwaway local database; the integration tests TRUNCATE it.
PG_DSN="postgresql://hivemind:hivemind@localhost:5432/hivemind"

cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}"

log() { echo "[session-start] $*" >&2; }

env_out() {
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "$1" >> "$CLAUDE_ENV_FILE"
  fi
}

# --- Python ---------------------------------------------------------------

export PATH="/usr/local/bin:$PATH"
if [ "$(uv --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]; then
  log "installing uv $UV_VERSION"
  python3 -m pip install --quiet --no-cache-dir "uv==$UV_VERSION" \
    || python3 -m pip install --quiet --no-cache-dir --break-system-packages "uv==$UV_VERSION"
fi

export UV_PYTHON="$PYTHON_VERSION"
export UV_PYTHON_PREFERENCE="only-managed"
uv python install "$PYTHON_VERSION"

# A .venv built by an older uv may sit on the 3.14 release candidate.
if [ -x .venv/bin/python ] \
  && [ "$(.venv/bin/python -c 'import platform; print(platform.python_version())' 2>/dev/null)" != "$PYTHON_VERSION" ]; then
  log "recreating .venv on Python $PYTHON_VERSION"
  rm -rf .venv
fi
uv sync --locked

env_out "export PATH=\"/usr/local/bin:\$PATH\""
env_out "export UV_PYTHON=\"$PYTHON_VERSION\""
env_out "export UV_PYTHON_PREFERENCE=\"only-managed\""

# --- Postgres + pgvector (best-effort) -------------------------------------

as_postgres() { runuser -u postgres -- "$@"; }

pgvector_ok() {
  local control="/usr/share/postgresql/$PG_MAJOR/extension/vector.control"
  [ -f "$control" ] || return 1
  local v
  v="$(sed -n "s/^default_version = '\(.*\)'/\1/p" "$control")"
  [ "$(printf '%s\n0.8.0\n' "$v" | sort -V | head -1)" = "0.8.0" ]
}

setup_postgres() {
  local bin="/usr/lib/postgresql/$PG_MAJOR/bin"
  [ -x "$bin/postgres" ] || { log "Postgres $PG_MAJOR is not installed"; return 1; }

  if ! pgvector_ok; then
    log "building pgvector $PGVECTOR_VERSION"
    if [ ! -f "/usr/include/postgresql/$PG_MAJOR/server/postgres.h" ]; then
      DEBIAN_FRONTEND=noninteractive apt-get update -qq
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "postgresql-server-dev-$PG_MAJOR" >/dev/null
    fi
    local src
    src="$(mktemp -d)"
    git clone --quiet --depth 1 --branch "v$PGVECTOR_VERSION" \
      https://github.com/pgvector/pgvector.git "$src/pgvector"
    make -s -C "$src/pgvector" PG_CONFIG="$bin/pg_config" >/dev/null
    make -s -C "$src/pgvector" PG_CONFIG="$bin/pg_config" install >/dev/null
    rm -rf "$src"
  fi

  if [ ! -f "$PG_DATA/PG_VERSION" ]; then
    log "initialising Postgres in $PG_DATA"
    install -d -o postgres -g postgres -m 700 "$PG_DATA"
    as_postgres "$bin/initdb" -D "$PG_DATA" -A trust -U postgres -E UTF8 --locale=C.UTF-8 >/dev/null
  fi

  if ! as_postgres "$bin/pg_ctl" -D "$PG_DATA" status >/dev/null 2>&1; then
    rm -f "$PG_DATA/postmaster.pid"
    as_postgres "$bin/pg_ctl" -D "$PG_DATA" -l "$PG_DATA/server.log" -w \
      -o "-c listen_addresses=localhost -p 5432 -k /tmp" start >/dev/null
  fi

  local psql=("$bin/psql" -h localhost -U postgres -d postgres -v ON_ERROR_STOP=1 -qtA)
  if [ "$("${psql[@]}" -c "SELECT 1 FROM pg_roles WHERE rolname='hivemind'")" != "1" ]; then
    "${psql[@]}" -c "CREATE ROLE hivemind LOGIN SUPERUSER PASSWORD 'hivemind'"
  fi
  if [ "$("${psql[@]}" -c "SELECT 1 FROM pg_database WHERE datname='hivemind'")" != "1" ]; then
    "${psql[@]}" -c "CREATE DATABASE hivemind OWNER hivemind"
  fi

  # Same database settings as the CI integration job.
  HIVEMIND_DATABASE_URL="$PG_DSN" HIVEMIND_EMBEDDING_DIM="512" \
    uv run hivemind-migrate >/dev/null
}

if (setup_postgres); then
  env_out "export HIVEMIND_DATABASE_URL=\"$PG_DSN\""
  env_out "export HIVEMIND_EMBEDDING_DIM=\"512\""
  log "ready: Python $PYTHON_VERSION, Postgres on localhost:5432 (db hivemind)"
else
  log "WARNING: Postgres setup failed; unit tests work, integration tests will skip"
fi
