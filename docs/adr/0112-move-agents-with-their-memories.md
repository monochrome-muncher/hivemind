# Move agents with their memories (fleet split)

Amends [ADR 0011](0011-fleets-and-trust-levels.md) (re-parenting never moves entries) and builds on [ADR 0107](0107-fleet-merge.md) (the fleet merge machinery, its locking and its undo). Touches [ADR 0058](0058-pinned-fleet-briefing.md) and [ADR 0096](0096-contributors-pin.md) (the pin cap).

Status: Accepted. Design from the 2026-10-27 session with Monochrome.

## Context

A fleet merge joins two fleets. The opposite need is a split: one team grows into two, or a few agents leave for another fleet. Today the only way is `PATCH /v1/admin/agents/{name}` per agent, and ADR 0011 leaves each agent's earlier fleet memories behind. The moved agent loses what it wrote, and the agents that stay keep it. The admin wants the movers to take what they wrote.

What the data looks like (see ADR 0107 for the rest):

- `entries.fleet_id` is set only for `fleet`-scoped entries. `self` entries follow their author and `org` entries have no fleet. Feedback and links are keyed by entry id, so they follow their entries.
- An entry has one author. A chain of versions (`superseded_by` links) can have several authors, because a privileged agent may supersede another agent's entry. A chain lives in one fleet (supersession needs the same fleet), though an earlier merge undo or move can leave a chain across two.
- Pins and search counters are keyed by fleet, not by agent.
- Read access is `entry.fleet_id == reader.home_fleet_id`, so what leaves a fleet is no longer readable by the agents that stay.

Requirements: the moving agents take exactly the fleet memories they wrote, with no picking; no new keys; undoable like a merge; a concurrent write must not land in the source after the move read its entry list; admin only, audited, in the panel as well as the API. Constraints: no new infrastructure, the admin key still writes only `self` and `org` (ADR 0060), no always-on text.

## Decision

### 1. Operation

`POST /v1/admin/fleets/{source_id}/move` with `{agents[], target_fleet_id | new_fleet_name, keep_pin_ids?, expected_pin_ids?, dry_run}`. Exactly one of the two targets is given, as for a merge. `dry_run` is required, so leaving it out never starts a move. Admin key only, audited as `fleet.move_agents`. A new target is created in the same transaction (audited as `fleet.create`). Splitting a fleet is moving some agents into a new fleet.

Every named agent must have the source as home fleet; otherwise 409 `move_refused` naming those that do not (an unknown name counts). The source and the target must differ (409 `move_refused`) and neither may be a merged fleet (409 `fleet_merged`). Moving every agent of a fleet is allowed. Unlike a merge, the source is not marked: it stays an ordinary fleet that still takes members and memories.

### 2. Which memories move

A memory moves when all of these hold:

1. it is `fleet`-scoped and filed in the source fleet (any state: active, superseded or withdrawn),
2. its author is one of the moved agents, and
3. the head of its supersession chain also moves, that is, the head also satisfies 1 and 2. The head is the entry at the end of the `superseded_by` links, or the entry itself when nothing supersedes it.

Rule 3 is the one exception the design asked for to keep chains whole: a mover's old version never leaves while its live version stays. Consequences, stated precisely:

- A mover's entry that a stayer superseded stays, because the head is the stayer's.
- A stayer's entry that a mover superseded stays, because its author is not a mover. The mover's head goes.
- In a chain mover, stayer, mover, the first and last go and the middle stays. The chain then crosses two fleets, the same shape an undo after later writes can already leave (ADR 0107). Both ends stay readable to anyone who can read both fleets. This is the rule as approved; it does not try to keep every chain in one place, only to never strand a mover's old version away from the live one.
- A mover's entry whose head lies in some other fleet stays. This includes a chain that an admin ended with an `org`-scoped successor: that head has no fleet, so the mover's fleet versions stay behind.

`self` notes and ratings are not fleet-bound, so nothing happens to them. Search counters are per fleet, so they stay with the source.

### 3. Pins

A pin follows its memory to the target, whoever pinned it. The pins at stake are the target's own pins plus the source's pins on memories that move. More than 10 of them need a `keep_pin_ids` choice of at most 10 (422 without it), exactly as in a merge (`choose_pins`, the newest 10 ticked in the preview). `expected_pin_ids` (the pins the preview showed) makes the move answer 409 `move_refused` ("preview again") when they changed since. Pins left out are unpinned, audited as `entry.unpin` with reason `fleet_move`, and recorded with the fleet they came from so an undo can restore them. The source's pins on memories that stay are untouched.

### 4. Preview

`dry_run: true` changes nothing and answers: both fleets' counts (agents, memories by state, pins), the moving agents, the agents that stay, how many memories leave (by state), how many of the movers' memories are held back by rule 3, and the pins in play with the newest 10 ticked. The panel shows in plain words that the memories leave the source and that the agents who stay lose access to them. There is no duplicate scan: unlike a merge, a move puts nothing next to anything that was not already in the target.

### 5. Locking and atomicity

Same as ADR 0107. In one transaction with `lock_timeout` of 5 seconds: both fleet rows `FOR UPDATE` in id order, then the moving agents' rows `FOR UPDATE`, then the pin locks in fleet id order. Every fleet write (entry insert, pin, activation, home-fleet change, `issue-agent`) takes `FOR SHARE` on its fleet row, so the move waits for writes in flight and blocks new ones until it commits; the entry list is read after the locks, so no write can land in the source after it. The agents' home fleet changes in the same transaction as the entries, so there is no moment where an agent lives in one fleet and its memories in the other. Waiting writers get 503 `fleet_busy` after 3 seconds as for a merge, and a move that cannot get its locks in 5 seconds or deadlocks answers 409 `move_refused` and changes nothing.

One gap a merge does not have: a merged source refuses every later write (`fleet_merged`), but a move leaves the source open. A write by a moved agent that resolved its old home fleet before the move committed would wait for the lock and then land in the source after it, filed under an author who now lives elsewhere. So the entry insert for a `fleet`-scoped entry, and the pin path, also compare the author's current home fleet with the fleet it is writing into, under the same fleet lock, and refuses a mismatch as `fleet_busy` (503, "the home fleet of X changed during this write, retry"). The client retries and lands in the new fleet. Cost: one primary-key read of `agents` per fleet write, in a transaction that already holds the fleet row. This also closes the same window for a plain `PATCH` home-fleet change. A mismatch for an author that is not a registered agent (the admin, tests) is ignored.

### 6. Record and undo: one table with a kind

A move is stored in the merge tables. Migration 0023 adds `fleet_merges.kind text NOT NULL DEFAULT 'merge' CHECK (kind IN ('merge', 'move'))` and widens `audit_log_action_check` with `fleet.move_agents` and `fleet.move_undo`. A move record uses the same columns (`moved_agents`, `dropped_pins`, `undone_at`) and the same `fleet_merge_entries` list. Existing rows read as merges.

This was chosen over separate `fleet_moves` tables because the undo rule is "the newest standing merge or move involving the fleet", and one table, one list and one ordering answer that with the code that exists. Two tables would need a union at every query, a merged ordering and a second undo path. The cost is a column on a small table and the word "merge" in table names. The column is additive (ADR 0020).

`POST /v1/admin/fleet-merges/{id}/undo` undoes either kind and `GET /v1/admin/fleet-merges` lists both, each with `kind`; there is no separate move undo route. The undo moves back exactly what was recorded: the entries still in the target go back to the source, the agents still in the target go back, pins on those entries go with them, and dropped pins are restored where their memory is again in that fleet and they fit (the oldest are skipped and reported). Pins that were on a returning memory are not carried back blindly: the source may have refilled its briefing since, so they are treated like restored pins. They are planned together with the source's dropped pins against the source's current pins (newest first, 10 per fleet and 3 per pinning agent, the admin exempt); those that fit move back, the others are removed from the target, because their memory is no longer there, audited as `entry.unpin` with reason `fleet_move_undo`, and listed in `pins_skipped`. It does not clear a merged mark, because a move set none. Memories written after the move stay in the target, even when they supersede a memory that goes back. Audited as `fleet.move_undo`.

An undo is allowed only for the newest standing (not undone) record involving the fleets, as target or as source. For a merge that is the rule of ADR 0107 (the target). For a move it is both fleets, because the entries go back into the source and nothing newer may have touched it either. It is refused while the target (and for a move the source) is merged away. A refused move undo is 409 `move_refused`; a merge undo stays `merge_refused`.

### 7. Admin panel

The Fleets view gets a "Move agents…" action next to "Merge into…". The dialog lists the fleet's agents with checkboxes, takes an existing fleet or a new name, previews (counts, the leaving and staying note, the pin checklist when there are pins in play) and asks the admin to type the source fleet's name before it applies. The list of merges becomes "Merges and moves", with a kind badge, and its Undo works for both. The proxy allowlist gets `POST admin/fleets/{id}/move`; the audit filter gains the two actions.

### 8. What doesn't change

No key is issued or rotated and nothing is written as a memory. The wire and MCP contract change only by the existing `fleet_busy` code now also covering a write that raced a move, so there is no new skill or always-on text. The plugin README says nothing about memories staying behind when an agent changes fleet, so it needs no change. Privileged agents read foreign entries as before.

## Consequences

- After `make rollback` past migration 0023, move records are gone, so a move cannot be undone any more. An older merge's undo then no longer sees the moves that followed it, and its newest-standing check can pass where it would have been refused. Roll back and undo in the right order: undo moves first.
- The ADR 0011 rule becomes: an entry's fleet is fixed at write time, except that an admin fleet merge re-files a whole fleet and an admin fleet move re-files the moving agents' own fleet memories, and each can be undone.
- The agents that stay lose access to the memories that leave. A privileged agent can still read them as foreign entries (ADR 0036). The preview says how many leave.
- Speed is as for a merge: re-filing updates `entries_fleet_idx` and re-inserts each row into the HNSW index. Both fleets stay locked meanwhile, so writes to them wait. The preview shows how many memories move so the admin can pick a quiet time. A move runs without the pool's command timeout like a merge.
- During a rolling deploy an older pod does not do the home-fleet check on writes, so a write that raced the move can land in the source for an author who left. Do not move agents during a rollout, as for a merge.
- Grafana series labelled with the fleet are not renamed; entry counts of both fleets change at once. The replica that ran the move drops its cached counts (the same hook as a merge).
- A chain split by rule 3 across two fleets is readable only by those who can read both; version grouping and supersession checks need a shared fleet (SPEC §6.3), so those features stop at the border.
- Risk class: data move, migration, permissions and locking. Tested on Postgres: writers racing a move, a write in flight across it, a write resolved before it, a move racing a home-fleet change of a mover, undo after later writes, the chain rule against both store adapters, pins over the cap, and the migration with its rollback.

## Considered options

- **Move by a time range or let the admin pick memories.** The user decided against picking: exactly what the movers wrote.
- **Move every memory of a mover's chain, even stayers' versions.** It keeps chains whole but takes other agents' memories with the movers. Rejected.
- **Leave a mover's superseded versions behind regardless of the head.** Simpler, but a chain's history would be split from its live version in the common case where one agent wrote both. Rejected for the head rule.
- **Mark the source as merged when all agents leave.** It would hide a fleet that can take new agents the next day. Rejected; the source simply stays.
- **Separate `fleet_moves` tables and a separate undo route.** Rejected for the reason in 6.
- **Skip the home-fleet check on writes and accept the race.** An admin's `PATCH` already has this window, but a move is a larger step with a visible effect (a mover's new memory filed in the old fleet and invisible to the movers' new teammates). One key read per fleet write is cheap. Rejected.
