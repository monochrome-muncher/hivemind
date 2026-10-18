# The admin panel shows who rated what

Extends [ADR 0080](0080-admin-memory-browser-and-activity.md) (the admin-only endpoints the panel's proxy forwards).

Status: Accepted. Admin-only and observation-only.

## Context

The admin panel shows feedback as counts: verdicts per day, verdicts given and received per agent, and the newest five rows on one memory (ADR 0051's `FEEDBACK_RECENT_LIMIT`). An operator cannot answer "what has this agent flagged?", "who called this author's work wrong?" or "show me every `wrong` this week" without reading the database. The `Store` could only list one entry's feedback.

## Decision

1. **One more admin-only endpoint**, `GET /v1/admin/feedback`. It lists feedback rows across every entry, newest first (`updated_at`, then entry id, reporter and agent), each with the entry it rates (id, kind, summary, author, scope, fleet, state). Filters: `reporter` (the registered reporter name), `verdict`, `author` (the entry's author), `fleet_id` (the entry's fleet), `entry_id`, with `limit` (1 to 100, default 50) and `offset` paging. A malformed `entry_id` or `fleet_id` matches nothing. Any key other than the admin key gets 403, as for the ADR 0080 endpoints, and the panel's proxy forwards it.
2. **Revoked reporters stay out**, as in every count (ADR 0067), so the log and the counts beside it agree.
3. **Observation only.** Nothing in search, ranking, scoring or anything an agent sees reads the log.
4. **The read follows the caller's visibility.** `Store.list_feedback_log` takes a `Visibility` like `list_entries` does, and the service passes the caller's. The admin key reads every row. The same service call can later serve an agent-key view of its own verdicts (rows on entries it can read only) without new SQL; exposing it to agent keys would be its own decision (SPEC §9).

## Consequences

- No index serves the order or the reporter and author filters: a read scans `feedbacks` (joined to `entries`) and keeps the top rows. That is fine for an admin page; an agent-facing use at volume would need an index on `feedbacks (updated_at DESC)` first.
- Notes are agent-written text. The panel shows them as plain text, like everything else entry-sourced (ADR 0043).
