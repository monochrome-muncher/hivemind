-- ADR 0118: an agent can clean up its own work from the portal. The record
-- says who ran it: `self_service` is true for a cleanup the agent ran with
-- its own key, false for the admin's (every row written before this).
-- Withdrawals carry the same `admin cleanup <id>` reason either way, so an
-- older pod can still undo a self-service cleanup.
--
-- ADR 0020 expand-and-contract: one NOT NULL column with a constant default
-- is additive and rewrites nothing. An older sibling pod ignores it, so
-- during a rolling deploy it lists a self-service cleanup as an admin's.
SET LOCAL lock_timeout = '5s';
ALTER TABLE agent_cleanups ADD COLUMN IF NOT EXISTS self_service boolean NOT NULL DEFAULT false;
