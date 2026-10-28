# Clean up after a bad agent

Extends [ADR 0067](0067-a-revoked-agents-feedback-no-longer-counts.md) (a revoked agent's feedback no longer counts) with a second reason to skip a rating, and follows the shape of [ADR 0107](0107-fleet-merge.md) (preview, guard, record, undo). Builds on [ADR 0098](0098-relied-on-changed-list-filter.md) (`withdrawn_at`) and [ADR 0096](0096-contributors-pin.md) (the pin caps). Amends SPEC §4.2, §12.4 and §12.5.

Status: Accepted. Design from the 2026-10-27 session with Monochrome. Amended by [ADR 0118](0118-agent-cleans-up-its-own-work.md): an agent can run the same cleanup on its own work from the portal.

## Context

An agent can go wrong for a while before anyone notices: a bad prompt, a stale checkout, a key used by the wrong tool. What it wrote in that time is in the pool, and agents read it. Today an admin has two tools. Revoking the key stops new writes, and ADR 0067 stops the agent's ratings from counting, but its memories stay active. Withdrawing memories one at a time (`POST /v1/admin/entries/{id}/withdraw`) does not scale to a bad afternoon, and it does not bring back the good memories the bad agent superseded. Those stay `superseded`, pointing at junk.

Facts the design has to respect:

- Entries are immutable. A memory is withdrawn or superseded, never edited or deleted (ADR 0098 keeps `withdrawn_at`).
- A rating is one row per reporter and memory, replaced when the reporter rates again (`updated_at`). Ratings shape `quality`, the `flagged` list and an entry's reports.
- Withdrawing a pinned memory unpins it, in the service layer that audits it (ADR 0096).
- A revoked agent's ratings are skipped by one SQL fragment on every feedback read (ADR 0067).

Requirements: the admin chooses a time window and sees what will change before it changes; ratings from the window stop counting without being destroyed; memories the bad agent superseded come back unless the admin says otherwise; the whole thing can be undone; revoking the key stays a separate, audited act.

## Decision

### 1. Operation

`POST /v1/admin/agents/{name}/cleanup` with `{since, until?, dry_run, restore_ids?, expected_withdraw?, expected_ratings?, also_revoke?}`. Admin key only; a non-admin gets 403 before anything is looked up. `since` is required and `until` defaults to now. `dry_run` is required, so leaving it out never starts a cleanup. The agent may be active or revoked, but it must exist (404).

**The window.** `since` is inclusive and `until` exclusive. It is applied to a memory's `created_at` (when it was written; `occurred_at` is the agent's own claim and can be anything) and to a rating's `updated_at` (when the rating was last written, so a rating the agent changed inside the window counts as in the window). A rating the agent gave before `since` and never touched since is not cleaned up.

Cleanup changes four things, in one transaction:

1. Every **active** memory the agent authored in the window is withdrawn, with `withdrawn_at` now and `withdrawn_reason` `admin cleanup <id>`. Superseded and already withdrawn memories are left as they are.
2. Every rating the agent gave in the window that is not already set aside is **set aside**: `feedbacks.set_aside_by` takes the cleanup id. The row stays.
3. Pins that show a withdrawn memory are removed in the same transaction and recorded, each audited as `entry.unpin` with reason `agent_cleanup`. A pin follows supersession (ADR 0096), so this includes a pin on an older version of a withdrawn memory whose chain ends at it, the way `PinService` unpins on a withdrawal. A pin on a memory that is being restored stays.
4. The admin's chosen `restore_ids` go back to `active` (`superseded_by` cleared). A memory is **restorable** when it is `superseded`, it is not one of the agent's own window memories, and following `superseded_by` from it passes only through the agent's own window memories and ends at one the cleanup withdraws. So with junk1 over good and junk2 over junk1, `good` is offered and junk1 is not; restoring `good` leaves junk1 superseded, out of normal search. A chain that passes through another author's memory, or ends at a memory that is not active, is not offered, so a replacement made by someone else is never undone. Each restored memory is recorded with the memory that replaced it and the head of that chain.

### 2. Preview

`dry_run: true` changes nothing and answers: the memories to withdraw (newest first, cut at 200, with the exact total), the ratings to set aside (exact total, by verdict), the pins that will come off, and the restorable memories (cut at 500, exact total). The admin panel always shows it first. Restorable memories are ticked by default; the admin can untick any of them. A window with nothing in it answers `nothing_to_clean` and an apply with nothing to do changes nothing and keeps no record.

### 3. Guard

The apply must carry the numbers the preview showed (`expected_withdraw`, `expected_ratings`); without them it is a 422. If either differs when the locks are held, the apply is refused with 409 `cleanup_refused` ("preview again"), so a memory or a rating that arrived in the window meanwhile is never cleaned unseen. A `restore_ids` entry that is not restorable at that moment is refused the same way.

### 4. Also revoke

`also_revoke` is off by default. When it is on, the agent is revoked in the same request, after the cleanup has committed, as its own audited `agent.revoke` through the same service path as the revoke endpoint, and the record notes it. The two are not one transaction on purpose: the revoke has its own key and lock handling (ADR 0028), and a cleanup that was done is worth keeping even when the revoke fails. If the agent is already revoked the answer says so and nothing more happens. The runbook tells the operator to replace or revoke the key first, since a live key lets the agent write more bad memories while the cleanup runs.

### 5. Undo

`POST /v1/admin/agent-cleanups/{cleanup_id}/undo` is admin only and audited as `agent.cleanup_undo`. It restores exactly what the cleanup changed, and only where nothing has moved on since:

- Withdrawn memories are re-activated only if they are still `withdrawn` with this cleanup's reason. A memory an admin withdrew again by hand, or one that someone else changed, is skipped and counted.
- Restored memories are superseded again by the memory that replaced them, only while they are still `active` and the head of that chain is active again after the undo; otherwise they stay active and are counted as skipped.
- Ratings carrying this cleanup's id are cleared. A rating the agent rewrote after the cleanup lost the mark already (see 7) and is not touched.
- The removed pins are put back newest first where they fit under the caps (10 per fleet, 3 per pinning agent, the admin exempt, the same rule as `plan_pin_restore` for a fleet merge); the rest are skipped and reported. Each restored pin is audited as `entry.pin` with reason `agent_cleanup_undo`.

Undo never un-revokes an agent. When the cleanup had revoked the agent, the answer says the agent is still revoked and re-activation is the admin's separate call.

Only the **newest standing** (not undone) cleanup of an agent can be undone. Otherwise a memory could have been touched by two cleanups and the record would not say which one to follow. Others answer 409 `cleanup_refused` naming the newer cleanup. `GET /v1/admin/agent-cleanups` lists cleanups newest first, each with `undoable` and, when false, `undo_blocked` with the reason. A cleanup that changed nothing keeps no record, so it never blocks an older undo.

### 6. The record

Migration 0022 adds `feedbacks.set_aside_by uuid NULL` and the table `agent_cleanups (id, agent, since, until, cleaned_by, cleaned_at, withdrawn_ids uuid[], restored jsonb, ratings_set_aside int, unpinned_pins jsonb, revoked bool, undone_at)` with an index on `(agent, cleaned_at DESC)`. The table has no foreign keys, like `fleet_merges` and `pins`. A side table keeps the hot `entries` table untouched. The same migration widens `audit_log_action_check` with `agent.cleanup` and `agent.cleanup_undo`, the way 0017 did. It is expand-only (ADR 0020); the rollback deletes the two audit action kinds, restores the CHECK, and drops the index, table and column. The rollback keeps the ratings themselves, so after a rollback they count again.

### 7. Set aside, and how it relates to ADR 0067

A set-aside rating is skipped wherever ADR 0067 skips a revoked agent's rating: `feedback_counts`, `quality_counts`, `list_feedback`, `list_feedback_many`, the `flagged` filter, `last_confirmed_at` and the recent reports. ADR 0067 expressed the rule as one SQL fragment that joins `agents`. That constant becomes `_RATING_COUNTS`, which adds `set_aside_by IS NULL` to it, so the two reasons share one place in `PgStore`, and `MemoryStore` applies both in one helper. ADR 0067's rule is unchanged: it is still reversible by re-activating the agent. The difference is that revocation hides all of an agent's ratings while it is revoked, and a cleanup hides the ratings of one window for good, until an undo, even if the agent is later active again.

An agent that rates the same memory again writes a new row version through the normal upsert, which clears `set_aside_by`. That is intended: a new rating is a new statement by an agent the admin now trusts, and it is current. Undo does not need to find it.

### 8. Concurrency

The cleanup takes, in this order: a per-agent advisory transaction lock `hivemind.cleanup:<agent>` (a second cleanup or an undo of the same agent waits for the first and, after the 5 second lock wait below, is refused with 409), the fleet pin locks of the fleets involved, then the memories and the restorable memories `FOR UPDATE` in id order. `lock_timeout` is 5 seconds like a fleet merge; one that cannot get its locks is refused with 409 `cleanup_refused` and changes nothing. A cleanup and an undo run without the pool's command timeout (as a merge does), because a big window is one transaction. The count guard in 3 is checked after the locks are held, so it sees what the cleanup will really change. The pin locks come before the entry locks, the order the pin path uses, so the two cannot deadlock.

### 9. Admin panel

The agent's page gets a "Clean up…" button, for active and revoked agents. It opens a dialog with the window (since and until), a Preview button, the list of memories to withdraw, the ratings count by verdict, the restorable memories (ticked), the "also revoke" box (off), and a type-the-agent-name confirmation before Apply. A "Cleanups" card on the Agents view lists past cleanups with an Undo button where undo is allowed; undoing one that revoked the agent shows a warning that the agent stays revoked. The panel proxy allowlist gets the three routes, and the audit filter gets the two actions.

### 10. What doesn't change

No key is issued or rotated by a cleanup, and no memory is written. There is no MCP or wire change. Agents see the effect the way they see any withdrawal: the memories stop appearing and the restored ones appear again. No skill or always-on text is added, so the budget is untouched, and the plugin README has nothing to say. Agent feedback summaries (the counts an agent sees on a memory) change silently when ratings are set aside, as they do when an agent is revoked.

## Consequences

- The ADR 0067 rule is now stated as: a rating is skipped when its reporter is revoked, or when a cleanup set it aside. SPEC §4.3 says so.
- Cleanup is a data change on possibly many rows. Withdrawing updates `entries` and its indexes; expect a few seconds per 10,000 memories. The agent's memories are found through `entries_author_created_idx` (author, then `created_at`). `feedbacks` has no index on the reporter, so the rating count and the set-aside read the whole table; this is a sequential scan, acceptable for an admin action at the sizes involved, and an index on `("user", updated_at)` would slow every rating write for a rare job, so none is added. The restorable and pin queries are recursive and run without the pool's command timeout like the other long statements.
- Restoring is limited by what the record can know. A memory the bad agent superseded and that was then superseded again by someone else is not restorable. A memory withdrawn by hand before the cleanup stays withdrawn. An undo does not recover a memory that was changed after the cleanup, and it reports how many it skipped.
- Cleanups of two different agents do not order each other: the newest-standing rule is per agent. Their withdrawn sets are disjoint, since each memory has one author, but a memory one cleanup restored can be replaced by the other agent's chain later. An undo checks the state of each memory and the head of its chain and skips and counts what no longer fits, so undoing in any order is safe and at worst less complete.
- The revoke is attempted after the commit. If it fails, the cleanup stands and the answer says so (`revoke_error`) instead of failing the request.
- A rating the agent rewrote after the cleanup is its own again and counts. Ratings of the window that the agent never rewrote stay set aside until an undo.
- During a rolling deploy, an older pod does not know `set_aside_by`: it still counts a set-aside rating and its upsert does not clear the mark. Do not run a cleanup during a rollout.
- The revoke after a cleanup is a second step. If it fails, the cleanup stands and the answer says the revoke did not happen (`revoke_error`); run the revoke from the agents list. An agent revoked by someone else in the meantime counts as already revoked.
- A failed audit write after the commit answers 500 although the cleanup happened. Check the cleanups list before running it again; the apply guard also makes a repeat harmless, since the window then holds nothing to withdraw.
- A cleanup selects memories and ratings by `author` and reporter, the name that wrote them, not by who owns them after a retirement (ADR 0113). The bad output belongs to the agent that wrote it: cleaning up a retired agent still reaches the memories its successor now owns, and cleaning up a successor leaves its inherited memories alone.
- Risk class: data change across entries and feedback, migration, permissions and locking. Tested on Postgres: row-lock races against concurrent writes, two cleanups of one agent at once, the lock timeout, undo after later changes, the pin caps on undo, the migration and its rollback, and the same contract on `MemoryStore`.

## Considered options

- **Delete the ratings.** Simpler reads, but nothing could be undone and the audit trail would lose what was judged. Rejected for set aside.
- **Revoke the agent and let ADR 0067 hide its ratings.** That hides every rating the agent ever gave, including the good ones from before the incident, and an active agent cannot be cleaned that way. Rejected as the only mechanism; `also_revoke` stays an option.
- **Revoke inside the cleanup transaction.** One step, but it ties the revoke to a long data change and its failure to the cleanup. Rejected.
- **Window on `occurred_at`.** The agent chooses that value, so a bad agent could place its memories outside any window. Rejected for `created_at`.
- **Restore every superseded memory of the agent, whoever superseded it.** That would undo supersessions made by other agents on purpose. Rejected: only memories whose chain runs through the agent's own window memories to a withdrawn one are offered.
- **Offer only memories directly under a withdrawn one.** It would offer the agent's own superseded junk and miss the good memory under a junk-over-junk chain. Rejected for the chain walk.
- **Allow any cleanup to be undone.** A memory touched by two cleanups would have no single origin. Rejected for the newest standing cleanup, the rule fleet merges already use.
- **A foreign key from the record to the entries.** It would break tooling that truncates `entries`, like `fleet_merges`. Rejected.
