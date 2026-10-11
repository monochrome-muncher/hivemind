-- ADR 0078: count opt-in `explore` searches next to the searches and the
-- empty ones (ADR 0056). One more counter on the existing per-home-fleet
-- row, bumped by the same first-page upsert. Counts only: no query text,
-- no agent name.
--
-- ADR 0020 expand-and-contract: a new column with a default is additive. An
-- older sibling pod keeps upserting (fleet_id, searches, empty) and the
-- column takes its default; its rows simply do not count explore searches.
-- One ALTER, so the table is locked once, briefly, and the CHECK is
-- validated against the existing rows in the same statement; give up
-- rather than queue behind a long read (the migration then fails and is
-- retried).
SET LOCAL lock_timeout = '5s';
ALTER TABLE search_counts
    ADD COLUMN IF NOT EXISTS explore bigint NOT NULL DEFAULT 0,
    ADD CONSTRAINT search_counts_explore_check CHECK (explore >= 0 AND explore <= searches);
