-- Rollback (ADR 0020): drops the withdrawal time 0016 started recording.
-- The state and reason stay; the `relied_on_changed` filter of an older
-- release has no use for the date.
ALTER TABLE entries DROP COLUMN IF EXISTS withdrawn_at;
