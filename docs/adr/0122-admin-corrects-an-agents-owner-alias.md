# An admin corrects an agent's owner alias

Amends [ADR 0011](0011-fleets-and-trust-levels-home-fleet-scoping-with-a-four-level-privilege-ladder.md) and [ADR 0120](0120-admin-creates-an-agent-for-its-owner.md). Touches [ADR 0027](0027-audit-log-of-admin-actions.md) and [ADR 0119](0119-rollbacks-do-not-silently-erase-the-audit-log.md). Amends SPEC §5.1 and §12.5.

Status: Accepted. Asked for by Monochrome on 2026-11-07.

## Context

Since ADR 0120 the admin types the owner alias by hand when creating an agent. A typo there creates a second owner in the panel's Stats "users" tab (a user is an owner alias; there are no accounts), and nothing could fix it: `PATCH /v1/admin/agents/{name}` changed only the trust level and the home fleet.

## Decision

### 1. One more field on the PATCH

`PATCH /v1/admin/agents/{name}` takes an optional `owner_alias` beside `trust_level` and `home_fleet_id`. At least one of the three is required (422 `update_required`, message updated). The alias is checked as on creation: the shared bounds and hidden-character rule of ADR 0040 (`validate_owner_alias`), and not blank (422 `invalid_input`). There is no way to clear an alias; this is a correction, and a creation by the admin always has one. The value is stored as typed, as registration stores it, so a case-only fix (`jane` to `Jane`) is a real change.

### 2. Any status

It works on a pending, active or revoked agent: the owner alias is a contact field, not an access control, so there is nothing to protect by refusing a status. The PATCH has no status rule for the trust level or fleet either (the service sets them on any existing agent), so this matches.

A bad alias is refused before anything else is applied, as a bad fleet already is: a request that sends a bad alias with a valid fleet changes neither.

### 3. Audit, and the no-op

New action `agent.owner_set`, actor the admin key, target the agent name, detail `{"from": old, "to": new}` (`from` is `null` for an agent registered without an alias). Migration `0029` widens `audit_log_action_check`; its rollback deletes those rows and is guarded like every audit-deleting rollback (ADR 0119). The migration is numbered 0029 because 0028 is reserved for another change; the chain accepts the gap, as it already does at 0018.

Setting the value the agent already has writes no audit row. This differs from `agent.trust_level_set`, which today audits a PATCH that sets the same level; it matches `fleet.rename` (ADR 0109). Trust level was left alone.

The old value is read in the same transaction, under the agent row's lock, as the update (`Store.set_agent_owner_alias` returns the agent before and after), so two corrections at once cannot record a wrong `from`.

### 4. Where the alias is stored, and what follows

The owner alias is stored in exactly one place: `agents.owner_alias`. Nothing copies it:

- `credentials` carries the agent name (`user_id`, `agent_name`), not the alias.
- Entries carry `author` (the agent name) and feedback carries `user` (the agent name); neither holds an alias.
- The Stats users tab, `GET /v1/admin/activity` and the sibling and top-3-owner health figures (ADR 0089) all join to the agent row at query time.

So correcting the one column makes every figure follow at once, past and future, with nothing to backfill. The corrected alias is also the one used by re-registration (`_same_owner`).

### 5. What is not rewritten

Past `agent.register` audit rows keep the alias as it was typed, in their `detail`. The audit log is append-only (ADR 0027), and the new `agent.owner_set` row is the record that the value changed and what it was. Entries are immutable and hold no alias, so no memory is touched.

### 6. Panel; no CLI

The admin panel's agent buttons (the Agents list, the pending queue and the agent page share them) get **Edit owner**, for every status: a small dialog with the current alias filled in, a toast on success and a refresh. The audit page's action filter lists `agent.owner_set`; its detail shows `{from, to}` like the other changes. The `hivemind-keys` CLI has no command that sets a trust level or home fleet, so it gets none for the alias. The route stays admin only and the panel's proxy already forwards `PATCH admin/agents/*`; the portal does not change.

## Consequences

- A typo in an owner alias is fixed in one step and the Stats "users" tab merges the two owners.
- Rolling back past 0029 needs `--discard-audit-rows` if any `agent.owner_set` row exists; the corrected aliases stay.
