-- Rollback (ADR 0020): drops the search counters 0010 started recording.
-- Nothing else reads them.
DROP TABLE IF EXISTS search_counts;
