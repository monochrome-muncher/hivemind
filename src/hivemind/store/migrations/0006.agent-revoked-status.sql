-- ADR 0028: a third agent status, `revoked`. Before this migration a
-- revocation deleted the agent's credential and left `status = 'active'`,
-- so a revoked agent looked exactly like an active one.
--
-- Widen the named CHECK (the 0003 pattern), then backfill: an `active`
-- agent that holds no agent credential is a pre-0006 revocation.
--
-- ADR 0020 expand-and-contract: widening is additive. The backfill does
-- write a value an old sibling pod does not know; ADR 0028 accepts that
-- one-rollout window on the admin-only listing.
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_status_check;
ALTER TABLE agents
    ADD CONSTRAINT agents_status_check
    CHECK (status IN ('pending', 'active', 'revoked'));

UPDATE agents a
   SET status = 'revoked'
 WHERE a.status = 'active'
   AND NOT EXISTS (
       SELECT 1 FROM credentials c
        WHERE c.kind = 'agent' AND c.agent_name = a.name
   );
