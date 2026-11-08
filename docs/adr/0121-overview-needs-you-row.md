# The Overview opens with a "Needs you" row

Touches [ADR 0029](0029-admin-panel-runner-with-a-browser-held-key.md), [ADR 0093](0093-admin-flag-backlog.md), [ADR 0110](0110-agent-key-last-use.md) and [ADR 0117](0117-lock-your-own-key-from-the-portal.md).

Status: Accepted.

## Context

Four things wait on the operator, and each lives on a different page. A key locked from the agent portal (someone reported a leak) showed only as a badge and a filter option on the Agents page. Pending agents had a KPI tile. Idle agents were a filter, and the flag backlog was a count on the Health page. Nothing on the landing page said "look here first".

## Decision

1. **A row at the top of the Overview.** "Needs you" holds one warning tile per item, shown only when its count is above zero, in this order: keys locked from the portal (active agents with `key_locked`), pending agents, idle agents (`idle`, past `idle_after_days`), and flagged memories (flagged more than 14 days ago, memory still active). When none applies, the row is replaced by one quiet line, "Nothing needs you right now."
2. **Each tile links to the list that explains it.** `#/agents?activity=locked`, `#/agents?status=pending`, `#/agents?activity=idle` and `#/health?flags=old`. The locked-key tile comes first and has a heavier border, since a reported leak is the most urgent.
3. **The Agents page keeps its filters in the hash**, like Memories, Feedback and Health do: `status` (`pending`, `active`, `revoked`) and `activity` (`locked`, `idle`). Changing a filter updates the hash without adding history entries.
4. **No new endpoint.** The counts come from the agent list and the metrics the Overview already loads. The flag tile reads `metrics.health.flagged_backlog_14d`, the number the Health page shows, so it follows the same rules, and the existing contract that pins that count to `GET /v1/admin/flagged?older_than_days=14` still covers it.
5. **The "Pending agents" KPI tile is dropped.** It would repeat the Needs you tile. The pending queue below the row stays, since it is where an agent is activated.
6. **Observation only.** Nothing is revoked, replaced or ranked from these counts.

## Consequences

- The logic lives in `needs.js` (no DOM) so a test can run it as it ships.
- The 14 days of the flag backlog is the Health page's fixed figure, not a setting.
