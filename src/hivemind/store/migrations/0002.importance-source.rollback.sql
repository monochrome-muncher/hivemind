-- Legal rollback (ADR 0020): the column is additive and carries no
-- information that existed before this migration.
-- IF EXISTS mirrors the forward migration's IF NOT EXISTS, so this
-- stays idempotent (safe to re-run against a pool where it already ran).
ALTER TABLE entries DROP COLUMN IF EXISTS importance_source;
