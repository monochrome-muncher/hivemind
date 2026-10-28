-- Rollback (ADR 0020): the column goes; a self-service cleanup then reads as
-- an admin's in the list. Its withdrawals carry the same `admin cleanup <id>`
-- reason as any cleanup, so it can still be undone from the admin panel.
SET LOCAL lock_timeout = '5s';
ALTER TABLE agent_cleanups DROP COLUMN IF EXISTS self_service;
