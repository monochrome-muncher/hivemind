-- Rollback (ADR 0020): drops the explore counter 0015 started recording
-- (the CHECK goes with the column). The search and empty counters are kept.
ALTER TABLE search_counts DROP COLUMN IF EXISTS explore;
