# Rename a fleet

Amends [ADR 0011](0011-fleets-and-trust-levels.md) ("no rename"), and amends SPEC §5.1, §12.1 and §12.5.

Status: Accepted. Design from the 2026-10-27 session with Monochrome.

## Context

ADR 0011 left fleet rename for later. Since then a fleet's name has become something people read: the admin panel, the audit log, `hive_whoami` (`home_fleet_name`) and the agent portal all show it. A typo or a reorganisation used to mean creating a new fleet and merging into it (ADR 0107), which leaves a merged fleet behind and records a merge that was not one.

A fleet's id never appears in what agents write. `entries.fleet_id`, `pins.fleet_id`, `search_counts.fleet_id` and `agents.home_fleet_id` all hold the id, not the name. The name is a display label with a unique index (`fleets_name_key`), so renaming touches one row.

## Decision

`PATCH /v1/admin/fleets/{id}` with `{name}`. Admin key only, audited as `fleet.rename` with detail `{from, to}` and the fleet id as the target.

- The name follows the same rules as a new fleet's name (non-blank, bounded, no control or zero-width characters; ADR 0040). A bad name is 422 `invalid_input`.
- Names stay unique, compared exactly as create does (case-sensitive). A name another fleet has is 409 `fleet_exists`, the code `POST /v1/admin/fleets` and the merge route already use for the same clash. An unknown fleet is 404 `not_found`. A non-admin key gets 403 before the route looks at the fleet, so it learns nothing about which ids exist.
- A merged fleet can be renamed, for example to "old-ops (merged)". Renaming does not undo or change the merge.
- Renaming a fleet to its current name answers 200 with the fleet and writes no audit row, so a repeated click or a retried request leaves no trace.
- The id, the entries, pins, search counters and agents do not change, and no key changes. Agents see the new name in `hive_whoami` on their next call. Nothing in the agent surface, the plugin or the always-on text changes.
- The store method returns the fleet as it was and as it is, read under a row lock, so the audit row reports the name that was actually replaced even when two admins rename at once. A racing rename to the same new name loses on the unique index and is reported as 409.
- Migration `0019` only widens the `audit_log_action_check` constraint with `fleet.rename`. Its rollback deletes the `fleet.rename` audit rows before narrowing the constraint again. Renamed fleets keep their new names.
- The admin panel's Fleets list gets a **Rename** button on every row, merged fleets included, and the audit log shows the new name as the target.

## Consequences

- An older pod during a rollout does not serve the route, so the rename is unavailable until every pod runs this release. Nothing can be written by an older pod that the old schema cannot hold.
- There is still no fleet deletion (ADR 0011).
- Audit readers who join `fleet.create` rows by name should use the fleet id. A fleet's earlier names stay readable in the `fleet.rename` rows.
