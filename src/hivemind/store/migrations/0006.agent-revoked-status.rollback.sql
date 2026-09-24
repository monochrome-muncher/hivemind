-- Rollback (ADR 0020): destroys nothing. Before 0006 a revoked agent was
-- represented as `active` with no agent credential, so map `revoked`
-- back to exactly that before restoring the narrow CHECK. A revoked
-- *pending* agent (a rejected registration, ADR 0028) had no pre-0006
-- representation; it also becomes `active` with no key, i.e. a revoked
-- agent in the old model — still no access, name still reserved.
UPDATE agents SET status = 'active' WHERE status = 'revoked';
ALTER TABLE agents DROP CONSTRAINT IF EXISTS agents_status_check;
ALTER TABLE agents
    ADD CONSTRAINT agents_status_check
    CHECK (status IN ('pending', 'active'));
