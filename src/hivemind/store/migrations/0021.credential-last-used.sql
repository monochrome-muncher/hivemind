-- ADR 0110: when a key last authenticated a request, so an admin can list
-- agents that have gone quiet. Written at most once an hour per key.
--
-- ADR 0020 expand-and-contract: one nullable column, no default, so the
-- ALTER is a catalogue change that rewrites nothing and an older pod
-- ignores the column. NULL means "not used since this shipped".
--
-- Keys of active agents start at this migration's now(): "assume used at the
-- upgrade", so the idle list gives every existing agent a full grace period
-- instead of calling them all idle on day one. Other kinds stay NULL. The
-- backfill touches one row per active agent.
SET LOCAL lock_timeout = '5s';
ALTER TABLE credentials ADD COLUMN IF NOT EXISTS last_used_at timestamptz;
UPDATE credentials c SET last_used_at = now()
FROM agents a
WHERE c.kind = 'agent' AND c.agent_name = a.name AND a.status = 'active'
  AND c.last_used_at IS NULL;
