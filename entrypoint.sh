#!/bin/sh
# Hivemind image entrypoint (Kubernetes + docker-compose).
#
# Runs the idempotent migration (ADR 0013) before the runner; owned here,
# not in an initContainer (ADR 0018). A dim mismatch (ADR 0015) or an
# unreachable DB fails LOUDLY and the pod never serves. No retry here: the
# restart policy (k8s) / depends_on healthcheck (compose) owns recovery.
#
# Then execs the runner named by HIVEMIND_RUNNER, forwarding extra args
# (e.g. `hivemind-keys issue-admin`). Config: HIVEMIND_* env vars
# (config/.env.example).
#
#   HIVEMIND_RUNNER=api       REST surface (hivemind-api)
#   HIVEMIND_RUNNER=mcp-http  hostable multi-agent MCP runner (ADR 0010)
#   HIVEMIND_RUNNER=admin     admin panel UI + proxy to hivemind-api (ADR 0029)
#   HIVEMIND_RUNNER=migrate   idempotent schema migration (ADR 0013)
#   HIVEMIND_RUNNER=keys      key-management CLI (SPEC §8.1, ADR 0012)
#
# Default: mcp-http (the compose service sets no HIVEMIND_RUNNER).
set -eu

runner="${HIVEMIND_RUNNER:-mcp-http}"

# The migration pre-step (ADR 0018), cheap on an up-to-date pool. Skipped
# when the runner IS the migration, and for the admin panel, which has no
# database (ADR 0029).
#
# DEP-14: this shell is PID 1, which ignores SIGTERM without a handler, so
# a foreground `hivemind-migrate` (e.g. waiting on the ADR 0020 advisory
# lock) would run until SIGKILL. Run it in the background and `wait`, so
# the trap forwards the signal and exits 143. The runner is `exec`ed and
# becomes PID 1 itself.
run_migrate() {
  hivemind-migrate "$@" &
  migrate_pid=$!
  trap 'kill -TERM "$migrate_pid" 2>/dev/null || true; wait "$migrate_pid" 2>/dev/null || true; exit 143' TERM INT
  wait "$migrate_pid"   # `set -e`: a failed migration aborts with its status
  trap - TERM INT
}

case "$runner" in
  migrate)
    # Not exec'd: python as PID 1 would ignore SIGTERM (see above).
    run_migrate "$@"
    exit 0
    ;;
  admin) ;;
  *) run_migrate ;;
esac

exec "hivemind-${runner}" "$@"
