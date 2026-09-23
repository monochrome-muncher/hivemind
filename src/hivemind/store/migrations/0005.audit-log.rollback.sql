-- Rollback (ADR 0020): the table is additive and carries no information
-- that existed before this migration, so dropping it returns the schema
-- to exactly its 0004 shape (the indexes and constraints go with it).
--
-- It is NOT free: every audit row written since 0005 was applied is
-- discarded. If that history matters, dump the table first
-- (`pg_dump --table=audit_log`) — docs/ops-runbook.md. IF EXISTS keeps
-- this idempotent, mirroring the forward migration's IF NOT EXISTS.
DROP TABLE IF EXISTS audit_log;
