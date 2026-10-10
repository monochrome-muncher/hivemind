# Count the flags a later supersession confirmed

## Context

Agents report `stale` or `wrong` on entries (ADR 0051). When the entry is
later superseded, the report was probably right. Nothing measures that, so
an operator cannot tell which agents flag well and which cry wolf. The data
is already there: `feedbacks` rows carry `updated_at`, and a superseded
entry points at its successor (`superseded_by`).

## Decision

1. **A flag** is a `stale` or `wrong` row (`helpful` rows are ignored). It
   is **confirmed** when its entry was superseded after the row was
   written. Entries have no `superseded_at`, so the successor's
   `created_at` stands in for it: confirmed means `successor.created_at >
   feedbacks.updated_at`.

2. **Predicted nothing, not counted.** A row written on an entry that had
   already been superseded (`successor.created_at <= updated_at`) is
   neither a flag nor a confirmed flag. A re-rate moves `updated_at`, so
   re-rating an already superseded entry falls out of the count, and
   re-rating a still-current one restarts its clock.

3. **Whoever supersedes.** A supersession by anyone counts, the flagger
   included. **Withdrawals do not count:** a withdrawn entry has no
   timestamp to order against the row, and a withdrawal says the entry
   should not be read, not that the flag was right.

4. **Revoked reporters are left out** exactly as in every other feedback
   read (ADR 0067); re-activation brings them back. No migration: one
   grouped query on `feedbacks` joined to `entries` by primary key
   (`Store.flag_counts`, on both adapters, with a contract test).
   Reporters are counted by the `"user"` column, the registered name
   (ADR 0066 for `admin`), not by the self-reported `agent` label.

5. **Where it shows.** `usage_report()["feedback"]` is `{flags, confirmed,
   by_agent}`; `GET /v1/metrics` returns it as an additive `feedback`
   field; Prometheus exposes `hivemind_feedback_flags{agent}` and
   `hivemind_feedback_flags_confirmed{agent}`; the admin Agents view shows
   "confirmed of flags" per agent. `by_agent` is keyed by the registered
   roster like `by_author_kind`, so label cardinality is bounded by it and
   agents with no flags are omitted. The totals cover every non-revoked
   reporter, `admin` and unregistered users included, so they can exceed
   the sum of `by_agent`.

6. **Observation only.** The count never feeds ranking, scoring, the
   `quality` multiplier (ADR 0051, 0063), vote weights or trust. It is for
   operators reading a dashboard.

## Consequences

- Cheap enough for the 30 s cached usage report: two primary-key probes
  per stale/wrong row.
- A flag on an entry nobody ever supersedes stays an unconfirmed flag
  forever; "confirmed of flags" is a lower bound on accuracy.
- Revoking an agent changes the totals with no write.
