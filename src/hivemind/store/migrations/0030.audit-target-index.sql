-- transactional: false
--
-- ADR 0123: "what happened to agent X" reads the audit log by subject. Until
-- now the log had indexes on `actor` and `occurred_at` only, so every other
-- shape (a row that names the agent as its target, a withdrawal of its memory,
-- a fleet move that lists it) meant paging the whole log. Performance only, no
-- behaviour change; the new `target` and `subject` filters of
-- `GET /v1/admin/audit-log` and the portal history use them.
--
-- 1. `audit_log_target_idx`: the exact `target` filter, and the agent-named
--    targets of the subject query.
-- 2. `audit_log_withdraw_author_idx`: an admin's (or a successor's) withdrawal
--    of an agent's memory names the agent only in `detail.author`. Partial: no
--    other action carries it.
-- 3. `audit_log_move_agents_idx`: `fleet.move_agents` lists the moved agents in
--    `detail.agents`. A GIN index on that array (partial, a handful of rows
--    per move) serves the `?|` test.
-- 4. `audit_log_successor_idx`: a retirement names the successor in
--    `detail.successor`. Partial, same reason.
--
-- The subject query ORs one arm per shape, so the planner can BitmapOr these
-- with the existing `audit_log_actor_idx` (docs/ops-runbook.md has the
-- measurement).
--
-- `CONCURRENTLY` (hence `-- transactional: false`, ADR 0020 §6 / ADR 0025) so
-- nothing takes a write lock while sibling replicas append audit rows;
-- `IF NOT EXISTS` because a cancelled concurrent build leaves an INVALID index
-- behind that a plain re-run would collide with (reindex or drop it:
-- docs/ops-runbook.md).
--
-- Expand-and-contract (ADR 0020): new indexes are invisible to an older
-- sibling.
CREATE INDEX CONCURRENTLY IF NOT EXISTS audit_log_target_idx
    ON audit_log (target, occurred_at);

CREATE INDEX CONCURRENTLY IF NOT EXISTS audit_log_withdraw_author_idx
    ON audit_log ((detail->>'author'), occurred_at)
    WHERE action = 'entry.withdraw';

CREATE INDEX CONCURRENTLY IF NOT EXISTS audit_log_move_agents_idx
    ON audit_log USING gin ((detail->'agents'))
    WHERE action = 'fleet.move_agents';

CREATE INDEX CONCURRENTLY IF NOT EXISTS audit_log_successor_idx
    ON audit_log ((detail->>'successor'))
    WHERE action IN ('agent.retire', 'agent.retire_undo');
