-- Legal rollback (ADR 0020): an index carries no information that is not
-- already in the table, so dropping these destroys nothing — it only
-- returns `?history` and the default list to the scans that preceded 0008
-- (correct, but O(pool)).
--
-- `CONCURRENTLY` for the same reason the forward direction uses it (no
-- ACCESS EXCLUSIVE lock on `entries`); the forward file's
-- `-- transactional: false` directive governs both directions.
DROP INDEX CONCURRENTLY IF EXISTS entries_author_created_idx;
DROP INDEX CONCURRENTLY IF EXISTS entries_created_at_id_idx;
DROP INDEX CONCURRENTLY IF EXISTS entries_superseded_by_idx;
