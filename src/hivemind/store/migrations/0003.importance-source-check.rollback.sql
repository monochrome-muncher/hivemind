-- Legal rollback (ADR 0020): the constraint is additive and carries no
-- information that existed before this migration.
ALTER TABLE entries DROP CONSTRAINT IF EXISTS entries_importance_source_check;
