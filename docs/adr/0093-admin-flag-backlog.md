# The admin panel lists the flag backlog, oldest flag first

Extends [ADR 0080](0080-admin-memory-browser-and-activity.md) (the admin-only endpoints the panel's proxy forwards) and [ADR 0075](0075-fleet-health-metrics.md) (the flagged backlog and orphaned flagged counts).

Status: Accepted. Admin-only and observation-only.

## Context

The Health page counts active entries flagged more than 14 days ago and flagged entries whose author is gone (revoked, or no write in 30 days), but an operator could not see which entries those are. Memories has a "Flagged only" filter, newest write first, which is the wrong order for working through a backlog: the entry flagged longest ago is the one most in need of a look, and an entry with no owner will not be fixed by its author.

## Decision

1. **`GET /v1/admin/flagged`**, admin key only (403 otherwise), lists active entries with at least one standing `stale` or `wrong` verdict, ordered by their oldest standing flag, then id. Each row carries the entry's list fields, the oldest and newest standing flag, the verdict counts and `orphaned`. Filters: `fleet_id`, `older_than_days` (0 to 3650; entries first flagged more than that many days ago) and `orphaned`, with `limit` and `offset`. A malformed `fleet_id` matches nothing. The panel's proxy forwards it.
2. **The same rules as the health counts.** A flag is a standing verdict (a reporter's latest), revoked reporters are left out (ADR 0067), and `orphaned` means the author is a revoked agent or wrote nothing in the 30 days before the request. So "flagged over 14 days" and "no owner" list the entries the two Health counts count, by the same rules; the counts are cached with the usage report, so they can lag the live list for a while after a change.
3. **The Health page** shows the list under the metrics, with All, Flagged over 14 days and No owner, and a fleet filter, all kept in the page's hash. The two counts link to it. A memory opens in the drawer, where the operator can read the verdicts and withdraw it.
4. **Observation only.** Nothing agent-facing reads the list, and the panel does not act on it beyond the existing withdraw.

## Consequences

- The read groups every standing flag before it cuts a page. Flagged entries are few and only the admin panel reads this, so no index serves the order.
- The store call takes a visibility, like the feedback log (ADR 0090), and the row is a shared front-end piece (`lists.js` `flaggedRow`, ADR 0091), so an agent-key app could list the flags on what its agent can read.
