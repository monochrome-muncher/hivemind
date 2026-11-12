# Every withdrawal is audited

Amends [ADR 0027](0027-audit-log-of-admin-actions.md) (the audit log of admin actions, which left an
author's own withdrawal out) and builds on [ADR 0095](0095-agent-portal.md) (portal withdrawals are
audited) and [ADR 0113](0113-retire-an-agent-with-a-successor.md) (a successor's withdrawal is audited).

Status: Accepted.

## Context

ADR 0027 kept the audit log to actions that need admin privilege, so an author withdrawing its own
memory wrote nothing. Since then two kinds of withdrawal were added to the log anyway: one made from
the agent portal (`via: "portal"`) and one by a successor of a retired agent (`via: "succession"`).
That left one gap: an agent withdrawing its own memory through `POST /v1/entries/{id}/withdraw` or
`hive_withdraw`. So "who removed what, and why" could not be answered from one place, the agent's
History missed those withdrawals, and the admin Overview's "since you last looked" line could not
count them.

## Decision

1. **Every successful withdrawal writes one `entry.withdraw` row**, whatever the surface. The detail
   always holds `author` and `reason`, and says how the caller was entitled to it:
   * no `via`: the admin withdrew someone else's memory (as before);
   * `via: "author"`: the caller withdrew its own memory (new; also the admin key withdrawing a memory
     authored as `admin`);
   * `via: "succession"`: a successor withdrew an inherited memory (as before);
   * `via: "portal"`: a withdrawal from the agent portal (as before, its own route).
2. **No new action and no migration.** `entry.withdraw` and the `agent_key` actor kind already exist.
3. **Refusals still write nothing**, as for every other audited action.

## Consequences

- The audit log, the agent page's History and the portal's History show every withdrawal.
- One more small insert per withdrawal. Withdrawals are rare next to writes and searches.
- The audit log is no longer only admin actions; it already was not, since ADRs 0076, 0095 and 0113.
- Bulk cleanups (ADR 0111) keep their single `agent.cleanup` row; they do not go through this path.
