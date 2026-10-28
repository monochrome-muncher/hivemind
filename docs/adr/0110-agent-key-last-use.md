# An agent key records its last use

> **Amended by [ADR 0114](0114-key-last-used-in-whoami-and-the-portal-home.md):** last use is no longer admin-only; an agent sees its own key's value in `whoami` and on the portal home.

Touches [ADR 0012](0012-shared-org-key-with-admin-issued-agent-keys.md) (the credential model) and [ADR 0029](0029-admin-panel-runner-with-a-browser-held-key.md) (the admin panel), and adds a field to SPEC §5.1 `GET /v1/admin/agents` and a sentence to §8.1.

Status: Accepted. Design from the 2026-10-27 session with Monochrome.

## Context

An admin had no way to tell which agents still work. Keys of people who left or of agents that were replaced stay valid until someone remembers to revoke them. The panel showed when an agent last wrote (from `entries`), but an agent that only reads, or only rates, never shows up there.

Every request, on the REST API, the MCP server and the agent portal, checks its key in one place, `PgAuthenticator.verify` (`src/hivemind/store/auth.py`). That query already reads the key's row.

## Decision

1. **The key records its last use.** Migration `0021.credential-last-used` adds `credentials.last_used_at timestamptz`, null by default. Null means "not used since this shipped". The migration is expand-only: one nullable column with no default, so an older pod ignores it. It backfills the keys of active agents with the migration's `now()` ("assume used at the upgrade"), one row per active agent; other kinds stay null. So every existing agent gets a full grace period and the idle list is right from the first day.
2. **At most one write an hour per key.** `verify`'s existing lookup also returns whether the stamp is missing or more than an hour old, computed by the database clock. Only then does it run `UPDATE credentials SET last_used_at = now() WHERE key_hash = $1 AND (last_used_at IS NULL OR last_used_at < now() - interval '1 hour')`. Requests that find the stamp stale send one `UPDATE` each; the `WHERE` repeats the test, so only the first writes.
3. **Best effort, outside the request's transaction.** The update runs on the connection `verify` already holds, after the credential is resolved and before it returns. The statement has its own 1 second timeout, so a row lock held by someone else cannot stall a request for the pool's longer command timeout. A failure never fails the request: database errors, a dropped connection, a timeout and OS errors are logged (with the key fingerprint, never the key; at most once a minute per process) and swallowed, and the next request tries again. The catch is that narrow tuple, not a bare `except`, so a mistake in the statement still shows up in tests.
4. **Only keys that authenticate.** A key that resolves to no identity (revoked agent, pending agent, retired `user` key) is not stamped. Admin and org keys are stamped like agent keys; only agents are listed.
5. **`GET /v1/admin/agents` gains three fields.** `last_used_at` is the agent's key's value (null if never used or no key). `idle` is true for an active agent whose last use is `HIVEMIND_IDLE_AGENT_DAYS` or more days old. With no use recorded it counts from when the key was issued (`credentials.created_at`, returned by the list as `key_created_at` on the domain `Agent`), then from the agent's activation. So a freshly issued or replaced key on an old agent is not idle. `idle_after_days` repeats the setting. The panel has no config endpoint, and the server already knows the answer, so the list carries it instead of adding a route. Pending and revoked agents are never idle.
6. **`HIVEMIND_IDLE_AGENT_DAYS`**, a `Settings` field, default 30, at least 1.
7. **The panel** gets a "Last active" column (relative time, "never" if null) and an "Idle N+ days" filter on the Agents page, next to the existing revoke button. It lists only: nothing is revoked automatically.
8. **Observation only.** No ranking, trust level, scope or permission reads `last_used_at`.

## Cost

The hot path gains no query and no write on a normal request: the lookup returns one more boolean from the row it already reads, and the join to `agents` is unchanged. One request per key per hour pays one extra `UPDATE` by primary key on a connection it already holds, a few milliseconds. For N active keys that is at most N small writes an hour, however many requests they make. The write takes a row lock on the key's own row for the length of that statement, which no other path takes except key issue and revoke. Each stamp leaves a dead row version for autovacuum, N an hour; at a few hundred keys that is negligible. The migration's `ALTER TABLE` needs a brief exclusive lock on `credentials`. Queued behind a long transaction it would hold up key checks, and so every request, for up to its 5 second `lock_timeout`, then fail and be retried; run it when no long transaction is open. `tests/unit/test_agent_last_used.py` and `tests/integration/test_key_last_used_pg.py` pin that a second check within the hour writes nothing.

## Consequences

- Existing agents start from the upgrade time, not from their real last use, which was not recorded. An agent that really went quiet before the upgrade shows as idle only `HIVEMIND_IDLE_AGENT_DAYS` days after it. An agent activated after the upgrade and never used is counted from its activation date.
- The stamp is accurate to the hour. Two replicas stamping a key within the same minute is harmless.
- Rolling back to an older release leaves the column in place and ignored. To also run `make rollback` (which drops the column), roll the image back first. This release's `verify` reads the column, so dropping it while this release still serves makes every key check fail. The rollback touches no key.
- `list_agents` joins `credentials` on the agent's single key (unique index, ADR 0039), so the list is still one query.

## Considered options

- **Derive it from `audit_log` or `entries`.** Misses agents that only read, and writes an audit row per request.
- **Stamp on every request.** One write per request on the hot path; rejected for the cost above.
- **Stamp from a background task.** Adds a queue and a failure mode for a value that is only read by a person; the hourly inline update is simpler.
- **A config endpoint for the panel.** More surface for one number; the list carries it.
- **Revoke idle agents automatically.** An agent that idles for a quarter and then matters would be cut off. The decision to revoke stays with the admin.
