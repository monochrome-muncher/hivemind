# An agent cleans up its own work from the portal

Amends [ADR 0095](0095-agent-portal.md) (what can be changed from the portal) and [ADR 0111](0111-agent-cleanup.md) (clean up after a bad agent, admin only). Builds on [ADR 0113](0113-retire-an-agent-with-a-successor.md) (what an agent owns). Amends SPEC §4.2, §5.1, §12.1, §12.5 and §9.

Status: Accepted. Design from the 2026-10-28 session with Monochrome.

## Context

ADR 0111 gave the admin a way to tidy up after an agent that went wrong: pick a window, preview, withdraw what it wrote, set its ratings aside, put back what it replaced, undo. The person behind an agent often sees the problem first. Their agent ran a bad prompt for an afternoon, and today they have to ask an admin to clean up work that is entirely their own agent's. The portal (ADR 0095) lets them withdraw one memory at a time, which does not scale to a bad afternoon and does not bring back the good memories the agent replaced.

The cleanup only touches the agent's own work, so nothing in it reaches another agent's memories or ratings. It is safe to let the agent run it for itself, with the same preview, guards and undo the admin has.

## Decision

### 1. The same operation, limited to the signed-in agent

The portal gets three routes, agent keys only (403 for the admin and org keys, as for every portal route):

- `POST /v1/portal/cleanup` with `{since, until?, dry_run, restore_ids?, expected_withdraw?, expected_ratings?}`. This is `POST /v1/admin/agents/{name}/cleanup` (ADR 0111) with the agent fixed to the key's agent and without `also_revoke`. The window, the required `dry_run`, the preview, the count guard (422 without `expected_*` on an apply, 409 `cleanup_refused` when they changed or a `restore_ids` memory is no longer restorable), the 5 second lock wait, "nothing to clean keeps no record" and the pin removal all work as in ADR 0111. The answer has no revoke fields.
- `GET /v1/portal/cleanups`: the agent's own cleanups, newest first.
- `POST /v1/portal/cleanups/{id}/undo`.

The code is the admin service (`AgentCleanupService`) and the admin store transaction (`Store.cleanup_agent`), called with a different set of authors. Nothing is forked. The wire answers reuse the admin shapes.

### 2. What it covers

- **Memories.** The active memories written in the window by **every author the agent owns** (ADR 0113): its own name and the agents retired in its favour. A successor that cleans up its own work cleans up the work it inherited too. The admin cleanup still goes by the one author name and does not reach a successor's inherited memories; it is unchanged.
- **Ratings.** Only the signed-in agent's own. The ratings of a retired agent are already skipped, because that agent is revoked (ADR 0067), and a successor cannot rate what it owns (ADR 0065), so nothing else could be set aside.
- **Restorable memories.** As in ADR 0111, now with the chain walk over all the owned authors' window memories. They are listed in the preview and ticked by default. They are limited to memories the agent's key can read (the visibility matrix, ADR 0011). A retired agent, or an agent that was moved to another fleet (ADR 0112), may have replaced a memory in a fleet the key cannot read; such a memory is not listed in the preview and cannot be restored, so the portal never shows or re-activates what the key could not read through the API.
- **Pins.** Taken off the withdrawn memories, as in ADR 0111, and audited as `entry.unpin`.

The store takes the cleanup lock of each owned author, in name order (the set is always the agent plus its owned authors, sorted), so a portal cleanup of a successor waits for an admin cleanup of the retired agent (and the other way round) instead of racing it. An undo takes the agent's lock and that of every author whose memories the cleanup withdrew, in the same order. Row locks already keep a memory from being withdrawn twice. When both ran over the same memory, the second finds it no longer active and skips it.

Lurkers (trust level 1) and agents that cannot write at all can run it: it only withdraws, sets aside and restores the agent's own work, and nothing in it needs a write scope. A pending or revoked agent has no working key and cannot sign in.

### 3. Who ran it

Migration 0026 adds `agent_cleanups.self_service boolean NOT NULL DEFAULT false`. The record's `cleaned_by` already holds the actor (an `admin:<fingerprint>` or an agent name), but a name alone cannot tell the two apart for certain, so there is a column. It records who ran the cleanup: the portal refuses to undo an admin's cleanup, and both lists show who ran each one. The reason on a withdrawn memory is `admin cleanup <id>` for both kinds, the existing text, so an older pod or a rolled-back release can still undo a self-service cleanup. The migration is additive; rollback drops the column and a self-service cleanup then reads as an admin's.

The audit is the same two actions, `agent.cleanup` and `agent.cleanup_undo` (and `entry.unpin` / `entry.pin` for pins), with actor kind `agent_key`, the agent's name as actor, and `via: "portal"` in the detail, as for a portal withdrawal (ADR 0095). The admin panel's cleanups list shows a self-service cleanup with a "self-service" mark and who ran it.

### 4. Undo, and who may undo what

The rule "only the newest standing cleanup of an agent can be undone" (ADR 0111) spans admin and self-service cleanups of that agent. Besides that:

- A **self-service** cleanup can be undone by the agent (portal) or by an admin.
- An **admin-run** cleanup of the agent cannot be undone from the portal: `POST /v1/portal/cleanups/{id}/undo` answers 403 `forbidden`. Only the admin can undo it. A cleanup of another agent answers 404, so the portal says nothing about it.
- The portal list shows the agent's admin-run cleanups too (marked "an admin", never undoable there), without the admin's identity. They explain why memories went missing and why an older self-service cleanup cannot be undone yet; the refusal names the newer cleanup.

### 5. Portal page

A "Clean up" page in the portal, built from the same pieces as the admin dialog: window, Preview, the memories to withdraw, the verdicts to set aside by kind, the older memories to bring back (ticked), typing the agent's name before it applies, and the list of past cleanups with Undo. There is no "also revoke" and no revoke of any kind. The text follows the glossary: it talks about the agent and the person behind it.

### 6. What doesn't change

No key is issued, replaced or revoked. No memory is written. The data plane, MCP and the plugin do not change. The portal still reads nothing the key could not read through the API: the preview lists only memories the agent owns, and the cleanup list holds nothing about other agents.

## Consequences

- An agent's owner can undo the effect of a bad afternoon without an admin, and an admin can still see and undo it. The audit log tells the two apart (`actor_kind` `agent_key`, `via: "portal"`).
- A person holding an agent key can withdraw all of that agent's work in a window in one step, where before they could withdraw one memory at a time. That was always within what the key owns; the preview, the typed name and the undo are the safeguards. The portal is still for the internal network (ADR 0095).
- A successor's self-service cleanup reaches the memories it inherited. An admin cleanup of the successor does not. This is deliberate: the successor owns them (ADR 0113), the admin goes by who wrote.
- Two cleanups over the same memory (an admin's of the retired agent and the successor's own) are not ordered by the newest-standing rule, which is per agent. An undo checks the state of every memory and skips and counts what no longer fits, as ADR 0111 already says for cleanups of different agents.
- During a rolling deploy an older pod does not know `self_service`: it lists a self-service cleanup as an admin's, and the portal routes do not exist there.
- Risk class: data change across entries and feedback, permissions, locking, a migration. Tested on Postgres (the lock across owned authors, the services end to end) and on `MemoryStore` through the shared contract, and at the HTTP seam for the 403/404 rules, the count guard, the audit rows and the undo rule across admin and self-service cleanups.

## Considered options

- **A cleanup the agent ran is undoable only by the admin.** Safer, but the point is to let the person recover without the admin. Rejected.
- **Let the portal undo an admin's cleanup of the agent.** The admin withdrew that work for a reason the agent may be the cause of. Rejected: only the admin undoes it.
- **Author-only, like the admin cleanup.** A successor could then not clean up the work it inherited, though it owns and can withdraw it one by one. Rejected for the owned authors.
- **Tell the two kinds apart by `cleaned_by`.** An agent name and an `admin:<fingerprint>` are different today, but a rule that depends on the shape of a name breaks the day the shape changes. Rejected for the column.
- **A separate withdrawal reason for self-service cleanups.** It would make an undo by an older pod, or after a rollback, find nothing to re-activate. Rejected: one reason, and the column tells the kinds apart.
- **An "also revoke" in the portal.** An agent revoking its own key from the portal would lock the person out. Rejected: revoking stays with the admin.
