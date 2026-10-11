# Pin integrity: audited pins and "changed since pinned"

> **Status note:** amended by [ADR 0079](0079-feedback-dates-on-the-database-clock.md): feedback `updated_at` now comes from the store's clock, like `pinned_at`, so the skew noted under Consequences is corrected.

Amends [ADR 0058](0058-pinned-fleet-briefing.md) (pins follow supersession
silently). Uses the audit log of [ADR 0027](0027-audit-log-of-admin-actions.md)
and [ADR 0046](0046-registrations-are-audited.md).

## Context

The pinned briefing is the widest channel in a fleet: every agent reads it
at the start of a session (ADR 0058). A pin follows supersession, so a
misled or hostile contributor who supersedes a pinned entry changes what
the whole fleet reads, and nothing says so. A pin or unpin is a privileged
act on the same channel, yet it leaves no record, unlike the admin actions
that ADR 0027 audits.

## Decision

1. **Pin and unpin are audited.** Every pin that is stored and every unpin
   that removed a pin writes one audit row: actions `entry.pin` and
   `entry.unpin`, `target` the entry id the caller named. `detail` is
   `{fleet_id}`, plus `from` on a pin that replaced older pins (the old pin
   id, or a list of ids when several were replaced), and `pin_ids` on an
   unpin (the ids of the pins removed). An unpin that removed nothing and a
   refused call write nothing. Never a key: the actor is the admin key
   fingerprint (`admin_key`) or, for a privileged agent, its
   server-verified name under a new actor kind `agent_key`. Two leads
   pinning the same entry at once each refresh the row in turn
   (serialised by the fleet lock), so each writes a truthful row.

2. **`changed_since_pin`.** Each entry of `hive_pinned` and `GET /v1/pins`
   carries `changed_since_pin: bool` and `change_reason`:
   `superseded` when the entry shown is a later version than the one pinned
   (`id != pinned_id`), else `flagged` when a `stale` or `wrong` verdict on
   the shown entry is newer than `pinned_at`, else none. `helpful` verdicts
   are not a change. It is a notice to the reader, who still reads the
   entry as data (ADR 0043); the pin stays until someone unpins or re-pins.

   **A re-pin acknowledges the change.** Pinning stamps a fresh `pinned_at`
   and `pinned_by`: re-pinning an already pinned entry refreshes its row,
   and re-pinning the version an older pin now shows (what ADR 0058 made a
   silent no-op) moves that pin to the new id. Either clears
   `changed_since_pin`. The briefing order rule of ADR 0058 is unchanged,
   newest pin first by `pinned_at`, so a re-pinned entry moves to the front.
   `Store.pin_entry` does it in one transaction under the fleet lock and
   reports whether the row was created and which pins it replaced; a swap
   never counts against the cap of 10.

3. **No new pin columns.** `pins` already keeps the originally pinned id
   and `pinned_at`; the flag check reads feedback `updated_at`. The
   feedback of every shown entry is read in one batched call
   (`list_feedback_many`, ADR 0059), the newest informative rows first, so
   a pinned list costs one extra read, not one per pin.

4. **Migration `0014.pin-audit.sql`** widens the two audit CHECKs (the 0009
   pattern): expand-only, with a rollback that deletes the pin rows it
   alone could hold.

5. **Observation only.** Neither the flag nor the audit rows feed ranking
   (ADRs 0057, 0058).

## Consequences

- An operator can ask the audit log who pinned or unpinned what and when;
  an agent reading the briefing sees that a pin is no longer what was
  pinned.
- A pin audited by agent name is only as strong as the agent key; the admin
  key rows carry a fingerprint.
- A stale or wrong verdict older than dozens of newer notes on the same
  entry can fall outside the flag check's window (50 rows per entry).
- A withdrawn pinned entry still shows `state: withdrawn` and no extra
  reason.
- The audit write follows the pin and is not atomic with it (ADR 0027). If
  it fails, the pin stays and the caller sees an error; a retry refreshes
  the pin and is audited, while an unpin retry finds nothing to remove and
  writes nothing (as for an admin withdrawal).
- `pinned_at` is the database clock (`now()`) and feedback `updated_at` the
  application clock, so skew between the two can mislabel a verdict written
  within moments of a pin. Not corrected: pin times stay on the database
  clock like every other row.
- After `0014`, rolling the image back without `make rollback` leaves
  `entry.pin` rows an older pod cannot parse, which breaks its audit view
  until they are removed (the rollback does that).
- An older sibling pod that lists the audit log during a rollout does not
  know the new values (the one-rollout window of ADR 0028).
