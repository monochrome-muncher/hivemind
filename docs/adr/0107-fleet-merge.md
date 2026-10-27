# Merge fleets into one home fleet

Amends [ADR 0011](0011-fleets-and-trust-levels.md) (an entry's fleet is fixed at write time; no fleet deletion), touches [ADR 0058](0058-pinned-fleet-briefing.md) and [ADR 0096](0096-contributors-pin.md) (the pin cap), and amends SPEC §12.1, §12.4 and §12.5.

Status: Accepted. Design from the 2026-10-27 session with Monochrome.

## Context

Two fleets can grow into the same work. Before this, the only way to bring their agents together was to move each agent with `PATCH /v1/admin/agents/{name}`. The home fleet is read from `agents.home_fleet_id` on every key check (`src/hivemind/store/auth.py`, no credential cache), so no new key is needed. But each agent's earlier fleet memories stayed in the old fleet (ADR 0011). So did the old fleet's pins (`pins.fleet_id`, migration 0012) and its search counters (`search_counts.fleet_id`, migration 0010). The moved agents lost what their fleet knew. Only a privileged agent could still read it, and then only as a foreign entry (ADR 0036).

What the fleet data looks like:

- `entries.fleet_id` is a uuid with no FK to `fleets`. It is set only for `fleet`-scoped entries. `self` entries follow their author, and `org` entries have no fleet.
- Read access is `entry.fleet_id == reader.home_fleet_id`. Supersession needs the successor in the same fleet. The `foreign_link` warning fires on a `see_also` link to another fleet. Version grouping needs a shared fleet (SPEC §6.3).
- Feedback and `entry_links` are keyed by entry id, so they follow their entries without any change.
- `fleets` held only `id`, `name` and `created_at`. There was no rename, disable or delete.
- Plugins, hooks and the skill hold no fleet ids.

Requirements: agents and their people should notice nothing; no new keys; admins can do it in the admin panel as well as through the API. Constraints: no new infrastructure; the admin key still writes only `self`/`org` (ADR 0060), and a merge writes no memory; no always-on text.

## Decision

### 1. Operation: merge a source fleet into a target fleet

`POST /v1/admin/fleets/{source_id}/merge` with `{target_fleet_id | new_fleet_name, keep_pin_ids?, dry_run}`. Exactly one of `target_fleet_id` and `new_fleet_name` is given. `dry_run` is required, so leaving it out never starts a merge. Admin key only, audited as `fleet.merge`. A new target is created in the same transaction (audited as `fleet.create`). That covers "two fleets into a new fleet": merge A into new C, then merge B into C. One source at a time keeps each merge and its undo small. The source and target must differ, and neither may be a merged fleet (409).

In one transaction, with `lock_timeout` of 5 seconds like the existing migrations (a merge that cannot get its locks is refused with 409 `merge_refused` and can be tried again):

1. Lock both `fleets` rows with `FOR UPDATE`, in id order.
2. Re-file every `entries` row with `fleet_id = source` to the target, whatever its state (active, superseded or withdrawn), and record each moved id in `fleet_merge_entries`.
3. Move every agent whose home fleet is the source to the target. Trust levels stay as they are. Record the names.
4. Re-key pins to the target. The admin's `keep_pin_ids` choice (see 3) decides which survive when the total is over 10. The others are unpinned, each audited as `entry.unpin` with reason `fleet_merge`, and recorded (with the fleet each belonged to) so an undo can bring them back. Pins can't collide, because an entry belongs to only one fleet.
5. Add the source's `search_counts` into the target's row and delete the source row.
6. Set `fleets.merged_into = target` and `merged_at = now()` on the source.
7. Insert the merge record. After the commit, write the `fleet.merge` audit row with the counts and the merge id.

**Concurrency.** A write that resolved its fleet before the merge committed could otherwise insert into the emptied source afterwards. The fleet entry insert, pin, activate, set-home-fleet and `hivemind-keys issue-agent` paths take `FOR SHARE` on their fleet's row and refuse a fleet whose `merged_into` is set (409 `fleet_merged`, naming the target; on MCP the error code is `fleet_merged`). The merge's `FOR UPDATE` waits for in-flight writes and blocks new ones until it commits. A write that loses the race is refused and retried by the client against the agent's new home fleet. Cost: one row lock per fleet write. The pin path takes the fleet row before its per-fleet pin lock, the order a merge takes them, so the two cannot deadlock.

### 2. Preview (`dry_run: true`), which the admin panel always shows first

- Counts: agents, memories by state and pins, for both fleets.
- All pins of both fleets, newest pin first, with the newest 10 marked as kept by default.
- Likely duplicate pairs: for each active entry of the smaller fleet (fewer active entries), its nearest active entry in the other fleet with cosine 0.90 or more (the near-duplicate cut-off of ADR 0087, `NEAR_DUPLICATE_SIMILARITY`). The lookup is capped at the newest 2,000 entries, and the preview says when it applies (`duplicates_truncated`). It is shown only. The merge changes no memory, and agents supersede over time as they do now.
- What the agents will see: a different `home_fleet_name` in `hive_whoami`, a longer memory pool, and the merged briefing. Nothing else.

### 3. Pins over the cap

The preview ticks the newest 10, and the admin can change which are ticked. More than 10 pins across the two fleets need `keep_pin_ids` (at most 10, each a pin of either fleet); without it the merge is a 422. With 10 or fewer and no `keep_pin_ids`, all are kept; the admin can still drop some. Pins that are not kept are unpinned and audited, and undo restores them. The per-agent cap of 3 needs nothing, since each pin came from one fleet.

### 4. The source fleet afterwards

Migration 0017 adds `fleets.merged_into uuid NULL` and `merged_at timestamptz NULL` (additive, ADR 0020). A merged fleet:

- is left out of `GET /v1/admin/fleets` unless `?include_merged=true` (then it carries `merged_into` and `merged_at`). In the panel it shows greyed with "merged into X" behind a filter. It is also left out of the usage metrics and the activity report, which would otherwise list an empty fleet.
- is refused as a home fleet for activate, set-home-fleet and `issue-agent`, as a merge target, and as a merge source (409 `fleet_merged`).
- keeps its row, so audit rows and Grafana history still name it.

### 5. Undo

`POST /v1/admin/fleet-merges/{merge_id}/undo` is admin only and audited as `fleet.unmerge`. It moves back exactly what the merge moved: the recorded entries go back to the source (only those still in the target), the recorded agents go back (only those still in the target), and `merged_into` is cleared. Pins follow their entries: a pin on a moved entry goes back to the source with it, and the dropped pins are restored to the fleet they came from where their entry is again in that fleet. Memories written after the merge stay in the target, even when they supersede or link to memories that move back. Those chains stay readable to anyone who can read both ends, the same as a chain left behind by an agent move today. Search counters stay summed, since a sum can't be split. When restoring pins would put a fleet over 10, the undo skips the oldest restored pins and reports them.

Undo is only allowed for the newest standing (not undone) merge involving the target, as its target or its source, and only while the target is not itself merged. If C has since been merged into D, that merge has to be undone first (409 `merge_refused`). This keeps the per-entry record unambiguous. `GET /v1/admin/fleet-merges` lists merges newest first with an `undoable` flag.

The merge record lives in two new tables: `fleet_merges (id, source_fleet_id, target_fleet_id, merged_by, merged_at, moved_agents text[], dropped_pins jsonb, undone_at)` and `fleet_merge_entries (merge_id, entry_id)`, primary key on both columns. Neither has FKs, like `pins` and `entry_links`. A side table keeps the hot `entries` table untouched. Migration 0017 also widens `audit_log_action_check` with `fleet.merge` and `fleet.unmerge`, the way 0014 did.

### 6. Admin panel

The Fleets view gets a "Merge into…" action per fleet. It opens the preview with the target picker (existing fleet or new name), the pin checklist (newest 10 ticked, no more than 10 allowed) and the duplicate pairs, then asks for confirmation by typing the source fleet's name. A "Merges" list shows past merges with an Undo button where undo is allowed. The panel proxy allowlist gets the two new POST routes and `GET /v1/admin/fleet-merges`. The audit log filter gains `fleet.merge` and `fleet.unmerge`. Fleet pickers leave merged fleets out.

### 7. What doesn't change

No key is issued or rotated. There is no wire or MCP contract change beyond the new `fleet_merged` error code and no skill or always-on text, so the budget is untouched. The agent portal shows the new fleet's memories on the next request. Privileged agents read as before, and the former foreign entries of the other fleet simply become home entries. Admin writes stay `self`/`org`.

## Consequences

- The ADR 0011 rule becomes: an entry's fleet is fixed at write time, except that an admin fleet merge re-files a whole fleet, and its undo files it back.
- Grafana and Prometheus series labelled with the source fleet stop at the merge and continue under the target. The health metrics cache (`HIVEMIND_METRICS_ENTRY_COUNTS_TTL`, 300 s) shows the old split for up to five minutes.
- Speed: re-filing updates `entries_fleet_idx` and re-inserts each row into the HNSW index, because the update is not HOT. Expect a few seconds per 10,000 entries. The merge holds the two fleet rows locked meanwhile, so writes to those fleets wait. The preview shows the entry count so the admin can pick a quiet time.
- During a rolling deploy, an older pod does not know `merged_into` and skips the lock guard. Its entry insert or pin can land in the source fleet after the merge (the entry is then in a merged fleet that no agent reads, and an undo would not move it back), and an agent it assigns to a merged fleet gets 409 `fleet_merged` on its next write until an admin moves it (the one-rollout window ADR 0028 accepted for `revoked`). Do not merge during a rollout.
- Long runs: the merge and the undo run without the pool's command timeout (30 s) and any server statement timeout, since one statement re-files every entry. Fleet writes wait at most 3 s for the row lock, then answer 503 `fleet_busy` (REST, with `Retry-After`, and MCP) for the client to retry. A merge that cannot get its locks in 5 s, or that deadlocks with a concurrent change, answers 409 `merge_refused`; run it again. The dry-run duplicate scan is bounded (20 s server-side, then a tenth of the entries, then none, reported as truncated).
- The panel sends the pins it showed as `expected_pin_ids`; the merge answers 409 `merge_refused` ("preview again") when the pins of the two fleets changed since. It sends `keep_pin_ids` only when a choice is needed or the admin unticked a pin.
- An undo restores dropped pins newest first, up to 10 per fleet and 3 per pinning agent (the admin is exempt); the rest are skipped and reported.
- Risk class: data move, migration, permissions and locking. Tested on Postgres: the race (a write in flight across a merge, and writers racing a merge), undo after post-merge writes, chained-merge refusal, pins over the cap, and the migration with its rollback.

## Considered options

- **Alias the old fleet id to the target at read time.** No data moves, but every read, supersede, pin, warning and metric would have to resolve aliases forever. Rejected.
- **Re-file without an origin record.** Simpler, but a mistaken merge could only be fixed by hand. Rejected.
- **Keep all pins over the cap, or keep the newest 10 automatically.** The first makes the briefing long; the second can silently drop a lasting fact. Rejected for the admin's pick.
- **Supersede duplicates during the merge.** This needs new cross-fleet admin supersede rules, and it is the automatic merging rejected earlier. Rejected; pairs are shown only.
- **Delete the source fleet, or leave it as a normal empty fleet.** Deleting breaks undo and the audit trail. Leaving it invites agents back into it. Rejected for the merged mark.
- **Split post-merge memories on undo by author.** That has to guess for admins and moved agents, and it can break chains. Rejected.
