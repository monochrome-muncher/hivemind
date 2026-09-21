# Entrypoint-owned idempotent migration (replaces the k8s initContainer)

The k8s deployment story (Tier 3.4) ran the idempotent schema
migration (ADR 0013) as a `migrate` **initContainer** on every pod
start. That worked, but it pinned the *deployment* to a specific
migration mechanism: the docker-compose dev service (and any other
container consumer of the image — one-off jobs, `kubectl run`,
local `docker run` smoke tests) had to duplicate the migration step
itself or skip it entirely.

## Decision

1. **The image entrypoint owns the migration (ADR 0018 →
   `entrypoint.sh`).** Before exec'ing the selected runner
   (`HIVEMIND_RUNNER`), the entrypoint runs the idempotent
   `hivemind-migrate` (ADR 0013) — cheap and a no-op on an up-to-date
   pool. The `migrate` runner skips the pre-step (it *is* the
   migration; no double-migration).
2. **The k8s `migrate` initContainers are deleted** (both
   Deployments) — migration has exactly one source of truth: the
   entrypoint.
3. **Failure stays loud (ADR 0015's posture is preserved).** A dim
   mismatch or an unreachable database fails the *first step* of the
   container (set -e), so the pod never serves — a crash-loop until
   the DB is up, never a silent runtime no-op.
4. **No retry in the entrypoint.** Recovery is owned by the
   deployment layer: the pod restart policy (k8s) / the compose
   `depends_on` healthcheck (dev). The entrypoint stays a thin
   pre-step + exec.
5. **The GitLab CI first-run key-bootstrap Job keeps its explicit
   `hivemind-migrate`** (the Job sets `command: [sh, -c, ...]`, which
   overrides the image entrypoint — it never runs `entrypoint.sh`,
   so the pre-step cannot cover it).

## Consequences

- `entrypoint.sh` runs `hivemind-migrate` before `exec
  hivemind-${HIVEMIND_RUNNER}` (skipped when the runner *is*
  `migrate`).
- The k8s `hivemind-api` + `hivemind-mcp` Deployments lose their
  `migrate` initContainers (DEPLOY.md §5/§6 + the troubleshooting
  rows move from "initContainer CrashLooping" to "entrypoint's
  migrate step exits non-zero").
- The docker-compose dev service migrates automatically on every
  start (gated by `depends_on: postgres: condition: service_healthy`).

## Alternatives considered

- *Keep the initContainers (status quo)*: two sources of truth for
  the same step, and the compose / one-off consumers still had to
  manage their own migration story.
- *A k8s Job + `kubectl wait` before the Deployments*: an extra
  deployment-layer step the k8s story would own — more moving parts
  for the same outcome, and it still doesn't cover compose / local.