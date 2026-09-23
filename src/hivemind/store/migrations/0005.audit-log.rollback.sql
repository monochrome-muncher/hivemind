-- Rollback (ADR 0020): drops the table only while it is EMPTY.
--
-- ADR 0020 does not write a rollback that destroys data, and for this
-- table the rule is not a formality: an audit log that one
-- `hivemind-migrate --rollback 1` can erase is an audit log anyone with
-- deploy access can erase without leaving a trace (ADR 0027). So on a
-- fresh deployment — nothing recorded yet — this reverses cleanly; once
-- a single row exists it refuses, and removing the table becomes a
-- deliberate, manual act: dump it first (`pg_dump --table=audit_log`,
-- docs/ops-runbook.md), then drop it by hand.
DO $$
BEGIN
    IF to_regclass('audit_log') IS NOT NULL
       AND EXISTS (SELECT 1 FROM audit_log) THEN
        RAISE EXCEPTION
            'refusing to roll back 0005.audit-log: audit_log holds rows, and dropping it would erase the audit trail (ADR 0027). Dump it (pg_dump --table=audit_log) and drop it manually if you really mean to.';
    END IF;
END
$$;
DROP TABLE IF EXISTS audit_log;
