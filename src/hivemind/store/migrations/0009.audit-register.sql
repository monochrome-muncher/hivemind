-- ADR 0046: registrations are audited. Widen the two named CHECKs on
-- audit_log (the 0003 / 0006 pattern): a new actor kind `org_key` (a
-- registration made with the shared org key) and a new action
-- `agent.register`.
--
-- ADR 0020 expand-and-contract: widening is additive. An older sibling pod
-- that lists the audit log during the rollout does not know the new values
-- (the same one-rollout window ADR 0028 accepted for `revoked`).
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_actor_kind_check;
ALTER TABLE audit_log
    ADD CONSTRAINT audit_log_actor_kind_check
    CHECK (actor_kind IN ('admin_key', 'cli', 'org_key'));

ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_action_check;
ALTER TABLE audit_log
    ADD CONSTRAINT audit_log_action_check
    CHECK (action IN (
        'agent.register',
        'agent.activate',
        'agent.trust_level_set',
        'agent.home_fleet_set',
        'agent.revoke',
        'fleet.create',
        'org_key.rotate',
        'entry.withdraw',
        'admin_key.issue',
        'admin_key.revoke',
        'agent_key.issue'
    ));
