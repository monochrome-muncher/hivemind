#!/usr/bin/env bash
# Regenerate src/hivemind/store/schema.sql from a migrated pool (ADR 0020).
#
# schema.sql is a GENERATED review aid, never applied or hand-edited; the
# migrations/ chain is the source of truth. CI fails if it is stale.
#
#   scripts/dump-schema-reference.sh [DSN] [OUTFILE]
#
# Migrate the pool at the default dim (1024, ADR 0015) first: the dump
# bakes in `vector(<dim>)`.
#
# Uses `pg_dump` from PATH if present, else the dev Postgres container
# ($PGCONTAINER, default hivemind-postgres-1).
set -euo pipefail

DSN="${1:-${HIVEMIND_DATABASE_URL:-postgresql://hivemind:hivemind@localhost:5432/hivemind}}"
OUT="${2:-$(dirname "$0")/../src/hivemind/store/schema.sql}"
PGCONTAINER="${PGCONTAINER:-hivemind-postgres-1}"

dump() {
  # yoyo's bookkeeping tables would churn the reference on every run.
  local args=(
    --schema-only --no-owner --no-privileges
    --exclude-table='_yoyo_*' --exclude-table='yoyo_lock'
  )
  if command -v pg_dump >/dev/null 2>&1; then
    pg_dump "${args[@]}" "$DSN"
  else
    docker exec "$PGCONTAINER" pg_dump "${args[@]}" "$DSN"
  fi
}

{
  cat <<'HEADER'
-- GENERATED FILE — DO NOT EDIT, AND DO NOT APPLY.
--
-- A readable snapshot of the schema the migration chain produces
-- (ADR 0020). The source of truth is src/hivemind/store/migrations/;
-- this file exists only so the whole storage model can be read and
-- diffed in one place. Regenerate with:
--
--     make schema-ref
--
-- yoyo's own bookkeeping tables (_yoyo_migration, _yoyo_log,
-- _yoyo_version, yoyo_lock) are excluded: they are the migration
-- mechanism, not the domain schema.
HEADER
  # Strip spurious diff: pg_dump's banner (version + timestamp), the
  # \restrict / \unrestrict nonces, and COMMENT ON SCHEMA public (present
  # only when the schema was re-created, as the integration suite does).
  dump \
    | grep -v '^--' \
    | grep -v '^\\restrict' \
    | grep -v '^\\unrestrict' \
    | grep -v "^COMMENT ON SCHEMA public IS" \
    | cat -s
} > "$OUT"

echo "wrote $OUT"
