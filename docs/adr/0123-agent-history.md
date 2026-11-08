# One agent's history, in the admin panel and in the agent portal

Builds on [ADR 0027](0027-audit-log-of-admin-actions.md) (the audit log), [ADR 0095](0095-agent-portal.md) (the portal), [ADR 0113](0113-retire-an-agent-with-a-successor.md) (what an agent inherits) and [ADR 0118](0118-agent-cleans-up-its-own-work.md) (an admin-run cleanup is shown without the admin). Amends SPEC §5.1, §12.5.

Status: Accepted.

## Context

"What happened to agent X" meant paging the whole audit log. `GET /v1/admin/audit-log` filtered by actor, action, time and a paging cursor only, and the table had indexes on `actor` and `occurred_at` alone. The rows about an agent come in several shapes: the agent's own key as the actor (portal withdrawals, pins, key lock, a self-service cleanup), the agent's name as the target of an `agent.*` or `agent_key.*` action, an `entry.withdraw` whose target is the memory's id and whose `detail.author` is the agent, a `fleet.move_agents` that names the agents it moved in `detail.agents` (its target is the source fleet), and a retirement that names the successor in `detail.successor`. Neither the admin's agent page (ADR 0091) nor the portal showed any of it.

What was checked in the code, per writer:

| Action | Target | Where the agent is named |
|---|---|---|
| `agent.register`, `agent.activate`, `agent.trust_level_set`, `agent.home_fleet_set`, `agent.owner_set`, `agent.revoke`, `agent.cleanup`, `agent.cleanup_undo`, `agent.retire`, `agent.retire_undo`, `agent_key.issue`, `agent_key.replace`, `agent_key.lock` | the agent's name | target; also the actor for `agent_key.lock` and a portal cleanup |
| `agent.retire`, `agent.retire_undo` | the retired agent | also `detail.successor` |
| `entry.withdraw` | the entry id | `detail.author`; the actor when the agent or its successor withdrew |
| `entry.pin`, `entry.unpin` | the entry id | only as the actor (an agent's own key); there is no `author` in the detail |
| `fleet.move_agents` | the source fleet id | `detail.agents` (names) |
| `fleet.move_undo`, `fleet.unmerge` | the source fleet id | `detail.agents` is a count: no agent is named |
| `fleet.create`, `fleet.rename`, `fleet.merge`, `org_key.rotate`, `admin_key.*`, `schema.rollback` | a fleet id, none, a fingerprint, a migration id | not about an agent |

## Decision

### 1. Two audit-log filters (admin)

`GET /v1/admin/audit-log` gains:

- `target=<exact>`: the row's `target` as stored.
- `subject=<agent name>`: the rows *about* the agent, one of
  - `actor_kind = agent_key` and `actor` the name;
  - `target` the name, for the actions whose target is an agent name (the first two rows of the table above);
  - `entry.withdraw` with `detail.author` the name;
  - `fleet.move_agents` with the name in `detail.agents`;
  - `agent.retire` or `agent.retire_undo` with `detail.successor` the name.

The shape checks are restricted by action and actor kind on purpose. A fleet or an entry whose id happens to equal an agent's name, or a CLI operator whose OS user is that name, is not the agent's history.

`AuditFilters` has `target` and `subjects` (a tuple: the portal passes the agent and the agents it inherits from); `AuditFilters.matches` and both stores implement them, with a store-contract case. The filters AND with the others and with the paging cursor.

Not found by `subject`: a pin or unpin of the agent's memory by someone else (the detail has no `author`), and an undo of a fleet move or merge (the detail holds only a count). Adding `author` to those rows would only help from then on, so it is not done here.

### 2. Migration 0030: the indexes

`0030.audit-target-index`, `-- transactional: false`, `CREATE INDEX CONCURRENTLY IF NOT EXISTS` (ADR 0025):

- `audit_log_target_idx` on `(target, occurred_at)`;
- `audit_log_withdraw_author_idx` on `((detail->>'author'), occurred_at) WHERE action = 'entry.withdraw'`;
- `audit_log_move_agents_idx`, GIN on `(detail->'agents') WHERE action = 'fleet.move_agents'`;
- `audit_log_successor_idx` on `((detail->>'successor')) WHERE action IN ('agent.retire', 'agent.retire_undo')`.

The author, move and successor indexes are partial: no other action carries those fields, so they stay a few thousand rows. The subject query is one OR arm per shape, each shaped like its index, so the planner can combine them with the existing `audit_log_actor_idx` in a BitmapOr. Rollback drops the four. It is additive (ADR 0020): an older pod never uses them.

Measured on a seeded table (600,000 rows: 300,000 admin trust changes, 200,000 agent pins, 100,000 withdrawals, 5,000 moves, 2,000 retirements; 5,000 agents; `ANALYZE`d), `subject=agent-42`, limit 50, custom plan:

`Bitmap Heap Scan` over a `BitmapOr` of `audit_log_actor_idx`, `audit_log_target_idx`, `audit_log_withdraw_author_idx`, `audit_log_move_agents_idx` and `audit_log_successor_idx`: 124 rows read, **0.7 ms**. A cached generic plan, which cannot see how rare one agent's rows are, walks `audit_log_occurred_at_idx` backwards and filters 232,000 rows: **79 ms**. asyncpg switches a cached statement to a generic plan after a few runs, so `PgStore.list_audit` runs the subject query inside a transaction with `SET LOCAL plan_cache_mode = force_custom_plan` (query-time settings are `SET LOCAL`, SPEC §6.2). `tests/integration/test_audit_subject_plan.py` pins that the plan is a BitmapOr over those indexes.

### 3. Admin panel

- The Audit log page gets an "About agent" field (`subject`) and a "Target" field, and reads `subject`, `actor`, `target` and `action` from the page address, so a link can open it filtered.
- The agent page gets a "History" section: the audit rows with `subject=<name>`, newest first, paged like the other lists, with a CSV export, and a link to the Audit log page filtered the same way. The row rendering is the Audit log page's, shared.
- The audit API pages by cursor, the shared tables by offset: a small adapter (`cursorPages` in `lists.js`) remembers the id that ends each position. The shared modules stay free of admin imports (ADR 0091).

### 4. Portal: `GET /v1/portal/history`

Agent keys only (403 for the admin and org keys, and for a dormant level 0 agent, as for the cleanups); on the portal allowlist, never on the admin allowlist and never forwarded by the admin proxy. `before` (the last row's `id`) and `limit` (1 to 100, default 50) page it, newest first. A revoked or locked key gets the usual 401 before it gets here.

The rows are `subjects = (the agent, *inherits_from)`. The row is turned into `{id, occurred_at, action, by, agent, about, summary, reason, entry_id}` through an allowlist (`domain/history.py`, no I/O):

- **Who.** `by` is `agent` for the agent key as actor, else `admin`: the admin, org and CLI actors collapse to "an admin", and the actor string is never copied. `agent` names which of the agent and its predecessors acted; the UI says "this agent" for the agent itself. An agent name appears only if it is the agent, an agent it inherits from, and for a successor named in a retirement only if that successor is one of those too (an undone retirement may name an agent the key does not own: "another agent").
- **Other agents.** A row can still match through `detail.author` after a retirement was undone and the memory's author handed to someone else, so an agent key outside the caller's own set may be the actor. That is `by: "another_agent"`: never named, never "this agent", and its withdrawal reason is hidden (it is that agent's words). The row's `actor` is read only for an `agent_key` actor, so a CLI operator whose OS user is named like an agent is never shown as, or linked to, that agent.
- **Paging.** With a `subject`, the `before` cursor row must itself be about the subject, else nothing matches.
- **Fleets.** A fleet is named only if the key can read it: its home fleet, or any fleet for a privileged agent (ADR 0011). Otherwise "another fleet". Fleet ids are never sent.
- **Entries.** `entry_id` is set only if the key can still read the entry (the visibility matrix, a withdrawn entry included); otherwise it is null. The portal links the id to the memory drawer.
- **Never.** Key fingerprints, owner aliases, cleanup, move and merge ids, pin ids, the other agents in a move, `target_fleet_name` as recorded (the live name is used when readable), and any detail key not in the table.

Per action, the line and what is read from `detail`:

| Action | Line | Detail read |
|---|---|---|
| `agent.register` | Registered, waiting for activation | none |
| `agent.activate` | Activated as `N (label)` in `<fleet>` | `trust_level`, `home_fleet_id` (as a fleet name or "another fleet") |
| `agent.trust_level_set` | Trust level changed from `N (label)` to `M (label)` | `from`, `to` |
| `agent.home_fleet_set` | Home fleet changed from `<fleet>` to `<fleet>` | `from`, `to` (as fleet names) |
| `agent.revoke` | Revoked; or "Revoked when it was retired in favour of `<agent>`" | `via`, `successor` |
| `agent.retire` | Retired; its memories now belong to `<agent>` | `successor` |
| `agent.retire_undo` | Retirement undone; its memories no longer belong to `<agent>` | `successor` |
| `agent.owner_set` | The owner recorded for it was changed | none (the aliases are dropped) |
| `agent_key.issue` | A key was issued | none |
| `agent_key.replace` | The key was replaced | none (fingerprints dropped) |
| `agent_key.lock` | The key was locked from the portal | none |
| `agent.cleanup` | Cleanup of `<since>` to `<until>`: N memories withdrawn, N ratings set aside, N restored, N pins removed | `since`, `until`, `withdrawn`, `ratings_set_aside`, `restored`, `pins_removed` |
| `agent.cleanup_undo` | Cleanup undone: N memories reactivated, N ratings put back, N pins restored | `reactivated`, `ratings_cleared`, `pins_restored` |
| `entry.withdraw` | Withdrew a memory; "A memory by `<agent>` was withdrawn" when an admin did; "Withdrew a memory inherited from `<agent>`" for a succession withdrawal | `reason` (shown as `reason`), `author`, `via` |
| `entry.pin` | Pinned a memory, in `<fleet>` | `fleet_id` |
| `entry.unpin` | Unpinned a memory, with the reason when it is one of the known codes (withdrawn, cleanup, fleet move or merge undo) | `reason` |
| `fleet.move_agents` | Moved from `<fleet>` to `<fleet>` | the row's target and `target_fleet_id` (as fleet names) |
| anything else | Something changed | none |

A withdrawal reason is free text written by whoever withdrew the memory, an admin's included, and is shown as plain text. That is the one place an admin's own words reach the page.

### 5. Portal page

A "History" page in the portal nav: the lines, newest first, paged, with a CSV export. Rows about a predecessor say which one. UI text names the agent and says "an admin"; it never uses the word "user".

## Consequences

- One agent's history is one indexed query in both surfaces, instead of a scan of the log.
- The portal learns what happened to the agent without learning who the admin is, which other agents were touched, or which fleets it cannot read. Pins and unpins by others, and undone moves, are not in it (section 1).
- A rollback of `0030` leaves `0027` applied, so it is the first rollback that writes a `schema.rollback` row (ADR 0119).
- `domain/history.py` is the only place that decides what the portal may say about a row. A new audit action shows as "Something changed" until it has a line in the table.
