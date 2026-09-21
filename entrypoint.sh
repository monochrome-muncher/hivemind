#!/bin/sh
# Hivemind image entrypoint (Kubernetes + docker-compose).
#
# ADR 0018: before starting the runner, the entrypoint runs the
# idempotent schema migration (ADR 0013). Migration is owned HERE
# (single source of truth) instead of a k8s initContainer: a dim
# mismatch (ADR 0015) or an unreachable database fails the FIRST
# step loudly — the pod never serves (crash-loop until the DB is up
# — the desired posture). There is NO retry in the entrypoint: the
# pod restart policy (k8s) / depends_on healthcheck (compose) owns
# recovery.
#
# Selects the runner via HIVEMIND_RUNNER and execs the matching
# console script, forwarding extra arguments (e.g. `hivemind-keys
# issue-admin`). The container inherits the HIVEMIND_* env vars from
# the deployment (k8s ConfigMap/Secret, or the compose service) — see
# config/.env.example for the full surface.
#
#   HIVEMIND_RUNNER=api       REST surface (hivemind-api)
#   HIVEMIND_RUNNER=mcp-http  hostable multi-agent MCP runner (ADR 0010)
#   HIVEMIND_RUNNER=migrate   idempotent schema migration (ADR 0013)
#   HIVEMIND_RUNNER=keys      key-management CLI (SPEC §8.1, ADR 0012)
#
# Default: mcp-http — so the existing docker-compose service (which
# sets no HIVEMIND_RUNNER) keeps working unchanged.
set -eu

runner="${HIVEMIND_RUNNER:-mcp-http}"

# The entrypoint-owned migration pre-step (ADR 0018): idempotent
# (ADR 0013) and cheap on an up-to-date pool. Skipped when the runner
# IS the migration itself (no double-migration; a bare `hivemind-migrate`
# run is a single, explicit migration).
if [ "$runner" != "migrate" ]; then
  hivemind-migrate
fi

exec "hivemind-${runner}" "$@"