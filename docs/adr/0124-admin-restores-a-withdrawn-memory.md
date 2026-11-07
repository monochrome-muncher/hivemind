# The admin restores one withdrawn memory

Amends [ADR 0001](0001-append-only-entries-with-explicit-supersession.md) (a withdrawal is no longer one-way) and [ADR 0080](0080-admin-memory-browser-and-activity.md) (the memory browser withdraws and now also restores). Builds on [ADR 0027](0027-audit-log-of-admin-actions.md), [ADR 0034](0034-supersession-targets-must-be-active.md), [ADR 0040](0040-input-bounds-and-agent-name-rules.md), [ADR 0091](0091-admin-panel-shared-front-end-modules.md), [ADR 0111](0111-agent-cleanup.md) and [ADR 0118](0118-agent-cleans-up-its-own-work.md). Amends SPEC §4.1, §5.1 and §12.5.

Status: Accepted.

## Context

A withdrawal (by an agent, the admin, a successor, or the person behind an agent in the portal) could only be reversed in bulk: by undoing the agent cleanup that did it (ADRs 0111, 0118) or a fleet merge or move. One mistaken withdrawal was permanent. The embedding and the text index of a withdrawn memory are kept, so nothing technical stops a single memory coming back.

## Decision

`POST /v1/admin/entries/{id}/restore` with `{reason}` (required, not blank, at most 2000 characters like a withdraw reason, ADR 0040). Admin key only: any other key gets 403 before anything about the entry is looked up, even with an empty body (`reason` defaults to empty on the wire and the service refuses a blank one with 422, after the admin check). The service checks the admin key itself, not only the route. The route is on the admin panel's allowlist and not on the portal's.

It puts the memory back to `active` and clears `withdrawn_reason` and `withdrawn_at`. The answer is the entry. **Pins are not restored**: withdrawing took the memory off the fleet briefing and the restore does not put it back; a person pins it again if they want it there. The answer to the admin and the panel say so.

It is audited as `entry.restore` (target: the entry id; detail `{author, previous_reason, reason}`). Migration `0031` widens `audit_log_action_check`.

The state check and the update run in one transaction that locks the memory's row first, so a withdraw, supersede or second restore of the same memory waits and then sees the new state. Two restores also take one transaction-scoped advisory lock between them, because the chain check below reads other rows.

### Refusals (409)

| Code | When |
|---|---|
| `entry_not_withdrawn` | The memory is active or superseded. |
| `restore_via_cleanup` | A standing (not undone) agent cleanup withdrew it: its reason is `admin cleanup <id>` and that cleanup's `withdrawn_ids` lists it. The message names the cleanup and points to its undo, which also brings back the rest of what the cleanup changed. A memory whose reason merely looks like that, with no standing cleanup listing it, is restorable. |
| `restore_chain_conflict` | A cleanup once put the memory back over a newer one, or restoring would otherwise risk two current versions of one thing (chain rules below). |

A fleet merge or move changes `fleet_id` only and withdraws nothing, and retiring an agent withdraws nothing, so none of them has an undo that a restore could contradict. Their undos never touch a withdrawn memory.

### Chain rules

A chain is the `superseded_by` links. The facts the code relies on:

1. Only an **active** memory can be withdrawn, so a withdrawn memory always has `superseded_by` unset.
2. Only an **active** head can be superseded (ADR 0034), so nobody can supersede a withdrawn memory, and a memory cannot gain a successor while withdrawn.
3. An older version of a withdrawn head stays `superseded` and keeps its link to it. Restoring the head makes it the one head again. This is the plain case and is allowed.
4. A restored memory is never `superseded`, so it never leaves an active memory with a successor.
5. The one way a chain loses its link is an agent cleanup: it withdraws the agent's head and, to bring back the good memory below it, sets that memory `active` with `superseded_by` cleared (ADR 0111). From then on nothing in the entries says that the two belong together, and withdrawn memories can come back (a restore, a cleanup undo), so no check of the successor's *current* state is safe: the successor can be withdrawn when the older memory is restored and active again a moment later (restore it, or undo a newer cleanup), leaving two active heads.

The rule is therefore deliberately conservative and uses the cleanup records, which keep for every memory they put back the memory that had replaced it (`restored[]`, any record, standing or undone):

- **Refuse to restore a memory a cleanup ever put back** (`restore_chain_conflict`, "write a new memory instead"). This holds whatever state its successor is in, and whether the cleanup stands or was undone.
- **Refuse to restore a memory that a currently active, cleanup-restored memory leads to**, found by walking forward from that record's recorded successor along `superseded_by`. (Defensive: the plain states do not produce it.)
- **Refuse if any active memory still names it as its successor**, which rules 1 and 2 say cannot happen.

What stays allowed is the plain case of rule 3, and a successor or head that a cleanup withdrew and that is restored on its own after the cleanup is undone (it is then an ordinary active memory with no cut involved).

### Redaction

A planned feature will blank the text of a withdrawn memory. A redacted memory has nothing left to bring back and must never become active. The refusal check in `domain/entry_restore.py` carries a comment where that state must also refuse; it is not implemented here.

### Panel

The memory drawer shows when a withdrawn memory was withdrawn (`withdrawn_at` is now part of the entry JSON, REST and MCP alike, null while the memory is not withdrawn) and, on the admin side only, a **Restore** button that asks for a reason. The drawer is a shared module (ADR 0091): the action is passed in by `admin.js` as `restoreEntry`, the portal never passes it, and a test pins that. The audit page lists `entry.restore` in its action filter and opens the memory from the target of `entry.withdraw` and `entry.restore` rows.

### Agents

Nothing changes in the tools. The memory is `active` again and appears in `hive_search` and `hive_list` as before. The skill never calls a withdrawal final, so it is unchanged.

## Consequences

- A mistaken withdrawal is recoverable, by the admin only. The author and the portal cannot restore.
- `Entry` gains `withdrawn_at`, read from the column that has existed since migration `0016`; no new column.
- The migration rollback for `0031` deletes `entry.restore` audit rows (guarded by `--discard-audit-rows`, ADR 0119); restored memories stay active. `0027` is no longer the head, so a rollback of `0031` alone leaves `0027` applied and writes a `schema.rollback` row, as ADR 0119 planned.
- Migration numbers 0028 to 0030 are held for other changes, so `0031` leaves a gap in the chain; the chain tolerates gaps (it already skips `0018`).
