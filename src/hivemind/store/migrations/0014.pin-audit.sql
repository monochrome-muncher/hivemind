-- ADR 0076: pins and unpins are audited. Widen the two named CHECKs on
-- audit_log (the 0009 pattern): a new actor kind `agent_key` (a privileged
-- agent of the fleet pinning inside its own fleet) and two new actions,
-- `entry.pin` and `entry.unpin`. The pins table already keeps the pinned
-- entry id and the pin time, which is all `changed_since_pin` needs.
--
-- ADR 0020 expand-and-contract: widening is additive. An older sibling pod
-- that lists the audit log during the rollout does not know the new values
-- (the same one-rollout window ADR 0028 accepted for `revoked`).
-- Both ALTERs take a brief ACCESS EXCLUSIVE lock; give up rather than queue
-- behind a long audit_log read (the migration then fails and is retried).
SET LOCAL lock_timeout = '5s';
ALTER TABLE audit_log DROP CONSTRAINT IF EXISTS audit_log_actor_kind_check;
ALTER TABLE audit_log
    ADD CONSTRAINT audit_log_actor_kind_check
    CHECK (actor_kind IN ('admin_key', 'cli', 'org_key', 'agent_key'));

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
        'agent_key.issue',
        'entry.pin',
        'entry.unpin'
    ));
