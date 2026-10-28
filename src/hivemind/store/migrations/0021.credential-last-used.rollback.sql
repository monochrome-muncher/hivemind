-- Rollback (ADR 0020): drops the last-use time 0020 started recording.
-- No key is touched; an older release never read the column.
SET LOCAL lock_timeout = '5s';
ALTER TABLE credentials DROP COLUMN IF EXISTS last_used_at;
