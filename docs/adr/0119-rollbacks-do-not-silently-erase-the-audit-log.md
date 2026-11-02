# Rollbacks do not silently erase the audit log

Amends [ADR 0020](0020-versioned-migrations-with-rollback-and-an-advisory-lock.md) (the rollback files) and [ADR 0027](0027-audit-log-of-admin-actions.md) (the audit log is append-only). Touches [ADR 0028](0028-revoked-agent-status-and-an-activation-guard.md) and [ADR 0039](0039-key-lifecycle-is-atomic-and-registration-is-owned.md). Amends SPEC §8.6 and §12.5.

Status: Accepted.

## Context

A shipped `.rollback.sql` is frozen (`tests/unit/migration_checksums.txt`). The rollbacks of `0009`, `0014`, `0017`, `0019`, `0020`, `0022`, `0023`, `0024` and `0025` each run `DELETE FROM audit_log WHERE action IN (...)`, because the older `CHECK` cannot hold the newer actions. One `hivemind-migrate --rollback N` therefore erased audit history without a word, while `0005`'s rollback already refuses to drop a non-empty audit log for the same reason (ADR 0027). The rollback of `0006` has a different quiet effect: it sets revoked agents back to `active`, which revives every credential row a revoked agent still holds, the "dead" credentials the migrate startup warning counts.

The files cannot be edited, so the guard has to live in the runner.

## Decision

### 1. The runner counts before it rolls back

`hivemind-migrate --rollback N`, under the migration advisory lock and before any DDL, works out the migrations the rollback would undo and counts what it would destroy:

- For each of them, the audit rows its rollback deletes. `ROLLBACK_AUDIT_DELETES` in `store/migrate.py` is an explicit table from migration id to the actions (and, for `0009` and `0014`, the actor kinds) its rollback deletes. A unit test cross-checks the table against the `DELETE FROM audit_log` statements of the shipped files, so it cannot drift, and fails for a rollback file that deletes audit rows and has no entry.
- For `0006`, the credential rows of agents whose status is `revoked`.
- For `0025`, the credential rows with `locked_at` set: its rollback drops the column, so a locked key becomes usable again (ADR 0117).

### 2. A non-zero count refuses, unless confirmed

With a non-zero count the command exits non-zero, names each migration with its actions and count, and changes nothing. Three explicit flags confirm, one per kind of loss: `--discard-audit-rows`, `--revive-revoked-keys` and `--unlock-locked-keys`. They are separate because the operator may accept one and not another. The command line is strict: `--rollback N` takes exactly one number and only these flags, and any other argument is refused. `make rollback` passes a flag only when `DISCARD_AUDIT_ROWS=1`, `REVIVE_REVOKED_KEYS=1` or `UNLOCK_LOCKED_KEYS=1` is given on the make command line.

### 3. The rollback is recorded

Migration `0027` widens `audit_log_action_check` with `schema.rollback`. After a successful rollback, if `0027` is still applied, the runner writes one row: `actor_kind` `cli`, the OS user as actor (as `hivemind-keys`), target the most recent migration id rolled back, detail `{rolled_back: [ids], discarded_audit_rows: n}` (the count only with the flag). If the rollback undid `0027`, the audit log can no longer hold the row and the runner logs it at WARNING.

`0027`'s own rollback deletes the `schema.rollback` rows, so it is covered by the same guard: it refuses while such rows exist unless `--discard-audit-rows` is given.

## Consequences

- The `schema.rollback` row is written only once a migration above `0027` exists and stays applied. Until then `0027` is the head, every rollback takes it along, and the WARNING on stderr is the only record of the rollback.
- Run a rollback with the app tier stopped. The counts are taken under the migration lock, which does not block live writes, so an audit row written between the count and the DDL is deleted without having been counted. `discarded_audit_rows` is the count taken before the rollback.
- A rollback past a populated audit action now needs a deliberate flag, and leaves a trace of itself.
- The counts are per migration and ignore overlap; `discarded_audit_rows` is the distinct total.
- A new migration whose rollback deletes audit rows must add its entry to the table; the test fails otherwise.
- `0005`'s rollback is unchanged: it still refuses to drop a non-empty audit log, flag or not.
