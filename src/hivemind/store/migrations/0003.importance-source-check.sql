-- Closes a gap 0002 left open: `importance_source` had no vocabulary
-- constraint, unlike every other enum-shaped column in this table
-- (`kind`, `state`; see also `credentials.kind`, `agents.status`).
-- `PgStore` does `ImportanceSource(row["importance_source"])`, so an
-- out-of-vocabulary value is an uncaught `ValueError` on read, not a
-- clean write-time rejection. Additive-only (ADR 0020 expand-and-contract):
-- every existing row is `'default'` (0002's DEFAULT), which satisfies
-- this constraint, and a not-yet-upgraded sibling project never writes
-- a different value.
ALTER TABLE entries
    ADD CONSTRAINT entries_importance_source_check
    CHECK (importance_source IN ('caller', 'default'));
