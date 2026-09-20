#!/bin/sh
# Hivemind image entrypoint (Kubernetes + docker-compose).
#
# Selects the runner via HIVEMIND_RUNNER and execs the matching
# console script, forwarding extra arguments (e.g. `hivemind-keys
# issue-admin` or a migrate subcommand). The container inherits the
# HIVEMIND_* env vars from the deployment (k8s ConfigMap/Secret, or
# the compose service) — see .env.example for the full surface.
#
#   HIVEMIND_RUNNER=api       REST surface (hivemind-api)
#   HIVEMIND_RUNNER=mcp-http  hostable multi-agent MCP runner (ADR 0010)
#   HIVEMIND_RUNNER=migrate   idempotent schema migration (ADR 0013)
#   HIVEMIND_RUNNER=keys      key-management CLI (SPEC §8.1, ADR 0012)
#
# Default: mcp-http — so the existing docker-compose service (which
# sets no HIVEMIND_RUNNER) keeps working unchanged.
set -eu
exec "hivemind-${HIVEMIND_RUNNER:-mcp-http}" "$@"