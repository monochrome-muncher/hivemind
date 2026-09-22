#!/usr/bin/env bash
# Regenerate src/hivemind/store/schema.sql from a migrated pool (ADR 0020).
#
# schema.sql is a GENERATED, NON-AUTHORITATIVE reference: it exists so a
# reviewer can read the whole storage model in one file and diff it
# across changes. It is never applied and never hand-edited — the chain
# under src/hivemind/store/migrations/ is the source of truth. CI
# regenerates it and fails if the committed copy is stale.
#
#   scripts/dump-schema-reference.sh [DSN] [OUTFILE]
#
# The pool MUST be migrated at the default embedding dim (1024,
# ADR 0015): the dump bakes it into `vector(<dim>)`, so dumping a
# 512-dim dev pool would commit a reference nobody deploys.
#
# Uses a local `pg_dump` when one is on PATH (CI installs
# postgresql-client); otherwise shells into the dev Postgres container
# ($PGCONTAINER, default hivemind-postgres-1) so a developer needs no
# client install.
set -euo pipefail

DSN="${1:-${HIVEMIND_DATABASE_URL:-postgresql://hivemind:hivemind@localhost:5432/hivemind}}"
OUT="${2:-$(dirname "$0")/../src/hivemind/store/schema.sql}"
PGCONTAINER="${PGCONTAINER:-hivemind-postgres-1}"

dump() {
  # yoyo's bookkeeping tables are the migration MECHANISM, not the
  # domain schema — they would make the reference churn on every run.
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
  # Strip three sources of spurious diff:
  #   - pg_dump's comment banner (carries a version + timestamp);
  #   - its \restrict / \unrestrict session tokens (random nonce);
  #   - COMMENT ON SCHEMA public, which is present only when the schema
  #     was RE-created (the integration suite drops and recreates it) and
  #     absent on a fresh database — i.e. it depends on how the pool was
  #     built, not on the schema.
  dump \
    | grep -v '^--' \
    | grep -v '^\\restrict' \
    | grep -v '^\\unrestrict' \
    | grep -v "^COMMENT ON SCHEMA public IS" \
    | cat -s
} > "$OUT"

echo "wrote $OUT"
