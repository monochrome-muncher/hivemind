-- Legal rollback (ADR 0020): an index carries no information that is not
-- already in the table, so this destroys nothing. The `target` and `subject`
-- audit-log filters stay correct and fall back to scanning by `actor` or
-- `occurred_at`.
--
-- `CONCURRENTLY` for the same reason the forward direction uses it, governed
-- by the forward file's `-- transactional: false`.
DROP INDEX CONCURRENTLY IF EXISTS audit_log_successor_idx;
DROP INDEX CONCURRENTLY IF EXISTS audit_log_move_agents_idx;
DROP INDEX CONCURRENTLY IF EXISTS audit_log_withdraw_author_idx;
DROP INDEX CONCURRENTLY IF EXISTS audit_log_target_idx;
