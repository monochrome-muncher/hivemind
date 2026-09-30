-- Rollback (ADR 0020): drops the two unique indexes and nothing else.
-- The duplicate keys the forward migration deleted are NOT restored.
DROP INDEX IF EXISTS credentials_one_org_key;
DROP INDEX IF EXISTS credentials_one_agent_key;
