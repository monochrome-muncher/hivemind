# Feedback is dated by the store's clock

Amends [ADR 0076](0076-pin-integrity.md) (its consequence that skew between
the pin and feedback clocks is not corrected).

Status: Accepted.

## Context

ADR 0076 flags a pin when a `stale` or `wrong` verdict is newer than
`pinned_at`. `pinned_at` comes from the database (`now()`), like every
`created_at`, but feedback `updated_at` came from the clock of the pod that
handled the call (`FeedbackService`). ADR 0076 recorded the skew and left
it. If a pod's clock lags the database, a verdict filed just after a pin is
dated before it and never flags it; if a pod runs ahead, a re-pin may not
clear a flag it should. The same mixed clocks feed the comparisons between
feedback and supersession (the rater metric of ADR 0068 and
the fleet health latencies of ADR 0075).

## Decision

1. **The store stamps feedback.** `FeedbackService` no longer sets
   `updated_at`. `Store.record_feedback` and `record_feedback_many` date a
   row that has none with the store's clock (Postgres `now()`, the
   `MemoryStore` clock) and return the rows as stored, so the reply shows
   the date that was kept. A batch shares one date, as before.

2. **A given date is kept.** A row that arrives with `updated_at` keeps it,
   so tests and imports can still write history.

3. **No schema change.** `feedbacks.updated_at` is unchanged; only where its
   value comes from moves.

## Consequences

- Every timestamp the flag check, the rater metric and the health metrics
  compare now comes from one clock, so pod skew cannot reorder them.
- Rows written before this change keep their application-clock dates; any
  skew in them stays.
- `Store` implementations outside this repository must stamp and return
  rows the same way (`tests/integration/test_store_contract.py` checks both
  adapters).
