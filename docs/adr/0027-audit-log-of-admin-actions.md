# An append-only audit log of admin actions, with two actor kinds

## Context

The admin surface (SPEC §12.4) changes who can do what. It activates
agents, promotes and demotes them, re-parents them, revokes their keys,
creates fleets and rotates the org key. The `hivemind-keys` CLI issues
and revokes admin and agent keys. Before this ADR none of these left a
record, so "who promoted this agent, and when?" or "which admin key
rotated the org key last night?" had no answer.

These changes arrive on two write paths that know different things:

1. **The app admin surface.** `AccessService`, plus
   `GovernanceService.withdraw` when an admin withdraws another agent's
   entry (SPEC §4.1). The caller has presented a verified admin key, but
   the credential could not say *which* one. Every admin key is issued
   with `user_id = "admin"` (`store/keys.py`, `store/auth.py`), and
   `Credential` carried no key identity, so an audit row would have read
   `admin` every time.
2. **The `hivemind-keys` CLI.** This is the bootstrap and break-glass
   tool. It writes to Postgres directly with `asyncpg.connect()` and
   hand-written SQL, and it bypasses `AccessService` and
   `PgAuthenticator` entirely. It has no credential at all. Its only
   "authentication" is holding the database DSN.

## Decision

1. **One insert-only table, `audit_log`** (migration `0005.audit-log`):
   `id`, `occurred_at`, `actor_kind`, `actor`, `action`, `target`
   (nullable) and `detail` (`jsonb`, default `{}`). It has indexes on
   `occurred_at` and `actor`. It has **named** CHECK constraints on
   `actor_kind` and `action`, the same pattern as every other
   enum-shaped column (migration `0003`). **No foreign keys:** `target`
   is a bare string, so a revoked agent's history outlives whatever
   happens to its record, and a key fingerprint never has to point at a
   `credentials` row that is gone. Nothing updates or deletes a row. The
   action vocabulary is: `agent.activate`, `agent.trust_level_set`,
   `agent.home_fleet_set`, `agent.revoke`, `fleet.create`,
   `org_key.rotate`, `entry.withdraw`, `admin_key.issue`,
   `admin_key.revoke` and `agent_key.issue`.

2. **Two actor kinds, because the two paths can prove different things.**
   * `admin_key`: a row written by the app admin surface. `actor` is
     `admin:<key fingerprint>` of the admin key the request presented.
     The server verified that key.
   * `cli`: a row written by `hivemind-keys`. `actor` is the new
     `--actor` option, which defaults to the OS user (or `uid:<n>` when
     the OS cannot name one). This name is **unverified**. Anyone with
     the DSN can run the CLI and type any name. Recording it is still
     useful, because honest operators name themselves. `actor_kind`
     exists so that nobody reads a `cli` actor as an authenticated
     identity. The CLI help says the same.

3. **`Credential` gains `key_id`, the key fingerprint.** The fingerprint
   is the first 12 hex characters of the key's **stored SHA-256 hash**,
   which is exactly what `hivemind-keys list` already displays.
   `PgAuthenticator.verify` computes it from the hash it already
   computes to look the key up. It is non-secret: it is a prefix of the
   hash, never of the key, and a hash prefix cannot be used as a key.
   `Credential.audit_actor()` renders `admin:<key_id>`, and falls back to
   `user_id` when `key_id` is `None` (dev mode, where no stored key backs
   the credential). This closes the "every admin row reads `admin`" gap
   without changing how admin keys are issued.

4. **Path 1 (app) writes after the mutation, not atomically with it,
   and a failed audit write is raised.** The mutation and the audit
   write go through two ports (`Store` and `Authenticator`, for example
   `activate` = `Store.activate_agent` + `Authenticator.issue_agent_key`).
   There is no shared transaction between those ports, and adding one
   would turn the port boundary into a transaction manager. The row is
   therefore written only after the mutation has succeeded. If the
   audit write itself fails, the error **propagates** to the caller.
   The action has already happened, and the caller must see that it
   went unaudited. Swallowing the error would make the log look
   complete when it is not. A denied action and a failed mutation write
   nothing.

5. **Path 2 (CLI) writes in the same transaction as the mutation.** The
   CLI holds one connection, so atomicity costs nothing there and it
   takes it: the change and its row commit or roll back together. A
   `revoke-admin` that matches no key changes nothing and records
   nothing. `target` is the agent name for agent commands and the key
   fingerprint for `issue-admin` / `revoke-admin`. `revoke-admin`
   accepts a raw key or a hash and normalises either one to the
   fingerprint.

6. **No raw key is ever recorded, and this is enforced by
   construction.** `activate`, `rotate_org_key`, `issue-admin`,
   `issue-agent` and `rotate-org` all produce a raw key, but the code
   that writes audit rows never receives it.
   `services/audit.record_admin_action` and the CLI's `_audit` take an
   action, a target and a detail, and have no parameter a key could be
   passed through. A test performs every key-producing action on both
   paths and asserts that no raw key appears anywhere in any audit
   row's serialised content, `detail` included.

7. **Deliberately not audited.**
   * `register` (`POST /v1/agents`, `hive_register`) is org-key
     self-service that creates only a *pending*, zero-privilege record.
     The privilege grant is `activate`, which is audited.
   * The read-only calls (`list_agents`, `list_fleets`, `hivemind-keys
     list`, and the audit log itself) change nothing.
   * An author withdrawing their own entry is governed by authorship,
     not admin privilege. `entry.withdraw` is recorded only when
     `credential.is_admin` and the entry's author is someone else.

8. **The read surface is `GET /v1/admin/audit-log`.** It is admin-gated
   and returns rows newest first. It filters on `actor`, `action`,
   `since` and `limit` (1–1000, default 100). There is no MCP verb: the
   log is an operator concern, the same reasoning as `GET /v1/metrics`.

## Consequences

* **A known gap on path 1: a crash between the mutation and the audit
  write.** If the process dies after, say, `Store.set_agent_trust_level`
  commits but before `record_audit` does, the change stands and no row
  records it. An audit-write *error* is loud (decision 4). A *crash* in
  that window is silent. The log is therefore a faithful record of what
  it contains, not a proof that nothing else happened. Closing this
  would need the mutation and the row in one transaction across the
  `Store` and `Authenticator` ports, and that is not worth the coupling
  at this scale.
* **A key-producing action can succeed with the key lost to the
  caller.** If `activate` or `rotate_org_key` issues a key and then the
  audit write fails, the request fails and the new raw key is never
  returned. For `activate`, the agent is active and holds a key nobody
  has, so the operator revokes it and re-activates (a new key). For
  `rotate_org_key`, the old org key is already dead, so the operator
  rotates again. The alternative, returning the key while hiding that
  the action went unaudited, contradicts decision 4.
* **CLI actors are claims, not identities.** Reports built on the log
  should filter or group by `actor_kind` before trusting `actor`.
* **Raw SQL bypasses the log.** A hand-run `DELETE FROM credentials`
  (the pre-`revoke-admin` fallback in DEPLOY.md §4.2) is not audited.
  The runbook steers operators to the CLI instead.
* **Rollback drops the table** (`0005.audit-log.rollback.sql`). Like
  `0002`'s rollback, which drops a column, this is legal under ADR 0020:
  the table holds nothing that existed before `0005`. It does discard
  every audit row written since, so the rollback file says to dump the
  table first if that history matters.
* **The log grows without bound.** There is no retention policy. At the
  rate admin actions happen, that is not a problem this ADR needs to
  solve. A retention job would be a new decision.
