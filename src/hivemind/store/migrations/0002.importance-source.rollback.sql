-- Legal rollback (ADR 0020): the column is additive and carries no
-- information that existed before this migration.
ALTER TABLE entries DROP COLUMN importance_source;
