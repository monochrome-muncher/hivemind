-- Legal rollback (ADR 0020): an index carries no information that is not
-- already in the table, so this destroys nothing. It returns the incoming
-- "see also" reads to 0011's single-column `entry_links_to_idx` (correct,
-- but it sorts every link to the entry) and the `agent` list filter to the
-- `entries_created_at_id_idx` walk.
--
-- Reverse order; `CONCURRENTLY` for the same reason the forward direction
-- uses it, governed by the forward file's `-- transactional: false`.
DROP INDEX CONCURRENTLY IF EXISTS entries_agent_created_idx;
CREATE INDEX CONCURRENTLY IF NOT EXISTS entry_links_to_idx ON entry_links (to_id);
DROP INDEX CONCURRENTLY IF EXISTS entry_links_to_created_idx;
