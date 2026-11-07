# Redact a withdrawn or superseded memory that holds a secret

Amends [ADR 0001](0001-append-only-entries-with-explicit-supersession.md) (entries are immutable) with one narrow, admin-only, irreversible exception. Builds on [ADR 0080](0080-admin-memory-browser-and-activity.md) (the admin memory browser), [ADR 0027](0027-audit-log-of-admin-actions.md) (the audit log), [ADR 0091](0091-admin-panel-shared-front-end-modules.md) (the shared drawer) and [ADR 0119](0119-rollbacks-do-not-silently-erase-the-audit-log.md) (the rollback guard). Touches [ADR 0107](0107-fleet-merge.md), [ADR 0111](0111-agent-cleanup.md), [ADR 0112](0112-move-agents-with-their-memories.md) and [ADR 0118](0118-agent-cleans-up-its-own-work.md) (undo paths). Amends SPEC §4.1, §5.1 and §12.5.

Status: Accepted.

## Context

Withdrawing a memory only flips its state. The summary, body, tags, sources, payload, extracted entities, the embedding and the keyword index all stay, and the admin memory browser still shows them. When an agent writes a credential or personal data into a memory, withdrawing it hides it from agents but leaves the secret in the database and in the admin panel. The only fix was hand SQL on production, with no audit row and no check that every copy had been cleared.

ADR 0001 keeps entries immutable so that "who knew what, when" survives. That reason does not extend to the text of a leaked secret: the provenance that matters is who wrote it, when, in which fleet, and who withdrew it, and all of that can stay.

## Decision

An admin can **redact** a memory that is already inactive: `withdrawn`, or `superseded`. Redacting wipes its text for good. An older version of a leaked secret (a v1 another agent superseded) is as readable through `?history` and `include_inactive` search as a withdrawn one, and withdrawing only takes active memories, so superseded memories are redactable directly. An `active` memory is not: withdraw it first.

### The route

`POST /v1/admin/entries/{entry_id}/redact` with `{reason, confirm}`. Admin key only, checked before the body is read. `confirm` must be the memory's id typed back (422 otherwise, as for an empty `reason`). 404 for an unknown id. 409 when the memory is `active` ("withdraw it first") and 409 when it is already redacted. The route is on the admin panel's allowlist, never on the portal's.

### What is wiped, in one transaction

| Field | After |
|---|---|
| `summary` | `[redacted]` |
| `withdrawn_reason` | `[redacted]` on a withdrawn memory (the reason may quote the text); a superseded one has none |
| `body`, `payload` | `NULL` |
| `tags`, `sources` | empty |
| `entities`, `entity_names`, `entities_model` | empty / `NULL` |
| `embedding`, `embedding_model` | `NULL` |
| `search_tsv` (the keyword column) | `NULL`, set after the update because the trigger refills it from `summary` |
| `entry_links` rows from this memory | deleted (they were chosen from its text) |
| `redacted_at` (new column, migration `0032`) | the time |

Kept: kind, scope, author, agent, fleet, importance, `occurred_at`, `created_at`, `state` (still `withdrawn` or `superseded`), `superseded_by`, the memories it superseded, the feedback rows and the audit trail. Links from other memories to it also stay, so a memory that linked to it still learns the target changed (ADR 0081).

**Feedback notes and flag notes are not edited.** They belong to other agents and may quote the text, but editing a rating changes its author's record and the flag counts. The response and the audit row count them (`feedback_notes`, and in the response `flag_notes`, the stale and wrong ones among them) so the admin can review them by hand; the runbook says how. The same goes for the audit rows of the memory that hold a withdrawal reason: the audit log is never edited, so the response and the `entry.redact` detail count them (`withdraw_reasons`): each `entry.withdraw` row with a non-empty `reason`, and each `entry.restore` row (ADR 0124) with a non-empty `previous_reason`, the withdrawal reason the restore copied from the memory.

The wipe is one `UPDATE` plus the keyword and link clean-up in one transaction under a row lock, so the memory is never half redacted and two redactions cannot both succeed.

### Nothing brings it back

Every path that sets a withdrawn memory active again refuses or skips a redacted one:

- the cleanup undo (ADRs 0111, 0118) re-activates only memories whose `withdrawn_reason` is the cleanup's reason and `redacted_at IS NULL`; a redacted one is counted in `reactivate_skipped`. The stub reason already stops the match; the column check does not depend on it.
- the fleet merge, move and undo (ADRs 0107, 0112) change `fleet_id` only, never `state`; a redacted memory goes along and stays withdrawn.
- withdrawing again and superseding already refuse a non-active memory.
- the agent-cleanup restore sets `superseded` memories active. A redacted superseded memory is neither offered by the preview nor restored (`redacted_at IS NULL` in the restorable set and in the update).

**A separate planned feature will add an admin "restore" route. It must refuse redacted memories**, with the same 409 as everywhere else.

### Reads

A redacted memory still reads: `state` is `withdrawn` or `superseded`, the text is the stub. The admin entry shape (`AdminEntryOut`, used by `GET /v1/admin/entries` and `GET /v1/admin/entries/{id}`, history included) adds `redacted: true` and `redacted_at`. The agent-facing shape, the REST `EntryOut` and the MCP `hive_get` result, does not change (`tests/unit/test_wire_contract.py` stays as it was): an agent that opens one sees an inactive memory whose summary is `[redacted]`. `?history` walks show the stub. Keyword and vector search skip it (its keyword column and embedding are `NULL`).

### Audit

A new action `entry.redact`, target the entry id, detail `{author, reason, feedback_notes, withdraw_reasons}`, and never the old text. The admin must not repeat the secret in the reason. The `entry.withdraw` audit row the earlier withdrawal wrote keeps its own `reason`, which the audit log never edits (ADR 0027); if that reason quotes the secret, it is the one place the operator still has to deal with by hand (the runbook says so). Migration `0032` adds `entries.redacted_at timestamptz` (nullable, no default, no backfill) and widens `audit_log_action_check` the way `0027` did.

### The panel

In the memory drawer, a withdrawn, not-yet-redacted memory gets a **Redact** button (danger style) when the app passed a `redactEntry` function to the shared drawer. The admin panel does; the agent portal never does, so the button cannot appear there. The dialog says it cannot be undone, asks for a reason and for the memory id typed back. A redacted memory shows a "Redacted <time>" badge. The audit page lists the action and reads its detail as a sentence.

## Consequences

- **Rollback cannot bring the text back.** `0032.entry-redact.rollback.sql` drops `redacted_at` and removes the `entry.redact` audit rows (`hivemind-migrate --rollback` refuses unless `--discard-audit-rows` is given, ADR 0119). Redacted memories stay withdrawn with their stub; the `[redacted]` withdrawal reason keeps an older release's cleanup undo from matching them.
- **Backups, WAL archives and replicas still hold the text** until they age out or are rebuilt. Rows the `UPDATE` replaced remain on the table's pages until vacuum reclaims them. The runbook lists the steps.
- Rows the update replaced stay on the table's pages and in the keyword, tag, entity and vector indexes until vacuum and a reindex, and in `pg_statistic` samples until an analyze. The runbook gives the `VACUUM (ANALYZE)` and `REINDEX INDEX CONCURRENTLY` steps.
- A secret that was ever readable must still be rotated at its source. Redacting cleans the pool; it does not un-leak.
- The audit log is still append-only, and it now has one more action whose rollback deletes rows (`ROLLBACK_AUDIT_DELETES`).
- An active memory is redacted after a withdrawal: the admin who finds a secret first withdraws it (hiding it from default searches), then redacts. Withdrawing alone is not enough, since `include_inactive` still reads it.

## Later notes

- **Restore refuses a redacted memory** (ADR 0124): 409 `entry_redacted`, in both stores, and the drawer shows Restore only on a memory that is not redacted.
- **Migration 0032 also creates `audit_log_redact_author_idx`** on `((detail->>'author'), occurred_at) WHERE action = 'entry.redact'`, so the admin `subject` filter of the agent history (ADR 0123) finds redactions of an agent's memories without a scan. The rollback drops it. The `audit_log_action_check` list in 0032 keeps `agent.owner_set` and `entry.restore` and adds `entry.redact`; the rollback restores 0031's list exactly.
- **Agent history** shows a row "An admin redacted a memory by <agent>": no admin identity, the entry id only when the viewer may read it, the reason only for the agent's own or inherited memories, and never the feedback or withdraw-reason counts.
