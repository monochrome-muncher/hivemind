#!/usr/bin/env bash
# Expand-and-contract guard (ADR 0020).
#
# `hivemind-api` and `hivemind-mcp-http` are deployed as independently
# released units against ONE pool, so a migration always runs against
# some still-deployed older code. The property that has to hold is not
# "no destructive DDL" — in correct expand-and-contract a DROP COLUMN is
# routine, it is the contract phase — but:
#
#     the previous N releases' code still works against HEAD's schema.
#
# So: migrate a database with HEAD's chain, then run each older
# release's own Postgres-backed store tests against it. An older
# release's `migrate()` is a no-op on a newer pool (its chain is a
# subset / its re-apply is idempotent), so letting those tests run
# unmodified is safe and keeps this honest.
#
#   scripts/check-backward-compat.sh [DSN] [N]
set -euo pipefail

DSN="${1:-${HIVEMIND_DATABASE_URL:-postgresql://hivemind:hivemind@localhost:5432/hivemind}}"
DEPTH="${2:-${COMPAT_RELEASES:-3}}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORKROOT="$(mktemp -d)"
trap 'cd "$REPO"; for w in "$WORKROOT"/*; do [ -d "$w" ] && git worktree remove --force "$w" 2>/dev/null || true; done; rm -rf "$WORKROOT"' EXIT

cd "$REPO"

# Release points: tags if the project uses them, else the preceding
# commits on this branch (AutoDevOps deploys per commit SHA).
mapfile -t POINTS < <(git tag --sort=-creatordate | head -n "$DEPTH")
if [ "${#POINTS[@]}" -eq 0 ]; then
  mapfile -t POINTS < <(git rev-list --skip=1 --max-count="$DEPTH" HEAD)
fi

if [ "${#POINTS[@]}" -eq 0 ]; then
  echo "no prior release points — nothing to check (first commit)"
  exit 0
fi

echo "==> migrating a fresh pool with HEAD's chain"
uv run hivemind-migrate

failures=0
for point in "${POINTS[@]}"; do
  short="$(git rev-parse --short "$point")"
  echo
  echo "==> release $point ($short): its store tests against HEAD's schema"
  wt="$WORKROOT/$short"
  git worktree add --detach --quiet "$wt" "$point"

  # Only the Postgres-backed store tests: they are what exercises the
  # schema. Unit tests cannot break from a schema change, and an older
  # release's migration tests assert ITS migration mechanism, not this
  # one, so they are deliberately excluded.
  mapfile -t suites < <(cd "$wt" && ls tests/integration/test_pgstore*.py 2>/dev/null || true)
  if [ "${#suites[@]}" -eq 0 ]; then
    echo "    (no store tests at $short — skipped)"
    git worktree remove --force "$wt"; continue
  fi

  if (cd "$wt" && uv sync --quiet && HIVEMIND_DATABASE_URL="$DSN" uv run pytest "${suites[@]}" -q); then
    echo "    OK: $short works against HEAD's schema"
  else
    echo "    FAIL: $short does NOT work against HEAD's schema."
    echo "    This is an expand-and-contract violation (ADR 0020): a column was"
    echo "    dropped, renamed or retyped while a still-deployed release uses it."
    echo "    Split it across two releases instead."
    failures=$((failures + 1))
  fi
  git worktree remove --force "$wt"
done

echo
if [ "$failures" -gt 0 ]; then
  echo "backward-compatibility FAILED for $failures of ${#POINTS[@]} release(s)"
  exit 1
fi
echo "backward-compatibility OK across ${#POINTS[@]} release(s)"
