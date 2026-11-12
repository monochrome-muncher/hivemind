# Scouts

Builds on [ADR 0011](0011-fleets-and-trust-levels.md) (agents and trust levels), [ADR 0027](0027-audit-log-of-admin-actions.md)
(the audit log), [ADR 0075](0075-fleet-health-metrics.md) (fleet health metrics), [ADR 0078](0078-explore-search.md)
(explore search) and [ADR 0143](0143-fleet-phases.md) (fleet phases). It keeps
[ADR 0043](0043-entries-are-untrusted-data.md) (entries are data, never instructions) intact.

Status: Accepted.

## Context

Honeybee colonies keep a small share of scouts that explore while most bees forage what is known
(Seeley, Honeybee Democracy, 2010). March ("Exploration and exploitation in organizational learning",
1991) found that organizations keep learning only if some members are slow to adopt the shared code.
Agents that share one memory tend to the opposite. Everyone reads the same settled entries, and nobody has the job of trying the rival
approach or re-checking the entry everyone relies on. A fleet phase (ADR 0143) asks the whole fleet to
explore for a while, but there is no way to ask one agent, or a few, to do it all the time.

## Decision

1. **An admin can mark an agent as a scout.** `PUT /v1/admin/agents/{name}/role` takes
   `{"role": "scout"}` and `DELETE` on the same path takes the role away. Both answer the agent, whose
   `role` is `null` when it has none; the admin agent list carries `role` too. The admin key only.
2. **A role is a hint, not a permission.** It changes nothing the agent may read or write, and it
   changes no ranking or search. `scout` is the only role for now.
3. **The agent reads a one-line hint.** `hive_whoami` (and `GET /v1/whoami`) carries `role: "scout"` and
   `role_hint` for an agent key whose agent is a scout, and neither key otherwise. The hint is fixed
   server text in `domain/agent_role.py` with the fleet's name filled in, or "your fleet" when the
   agent has none. It is not part of the tool descriptions or the skill, so the always-on text does not
   grow.
4. **Setting and clearing are audited** as `agent.role_set` and `agent.role_clear`, with the agent's
   name as target and `{role}` as detail (for a clear, the role it ended). Marking an agent that already
   is a scout, or clearing one that is not, changes nothing and writes no audit row.
5. **A revoked agent cannot be made a scout** (409 `invalid_status`, like the other lifecycle actions on a
   revoked agent). A pending agent may be marked. Clearing is allowed in any status.
6. **A role stays with its agent.** A fleet merge or move does not touch it. A retirement revokes the
   retired agent and leaves its role on the record; the successor does not inherit it, and an
   offboarding changes nothing about it.
7. **Observation only, for the admin.** The usage report counts the agents that are not revoked by role
   (`agents.by_role`, `{"scout": n}`) and `GET /metrics` serves `hivemind_agents_by_role{role}`. The
   health section gains `health.role_activity_30d`, `{"scout": {...}, "other": {...}}`, with the last 30
   days of each group's `writes`, `alternative_or_dead_end_writes`, `supersessions` (counted when the
   successor was written, by its author) and `verdicts` (non-revoked reporters, as the other verdict
   figures). An author or reporter with no agent record counts with everyone else. The gauges are
   `hivemind_health_role_activity_30d{role,motion}`.
8. **Migration `0034`** adds nullable `agents.role` with a CHECK that it is `scout` when set, and the two
   audit actions. It is expand-only.
9. **The admin panel** shows a Scout card on the agent page (whether it is a scout and a Make scout or
   Stop scouting button with a confirmation), a Scout badge in the Agents list, and on the Health page a
   short table of the 30-day comparison.

## Consequences

- **A nudge, not enforcement.** Nothing makes a scout explore and nothing checks that it did. The
  comparison table is how the admin sees whether the hint changes what the scouts write.
- **Entries remain data (ADR 0043); this hint is server text.** It is written by the server, fixed in
  code, and no entry can set or change it.
- **Ranking is untouched.** A scout's search results equal those of any other agent with the same
  access, so the retrieval eval and every ranking decision stand.
- **No always-on cost.** The hint is not a tool description or skill text, so it does not count against
  the always-on budget. Only a scout reads it.
- **The metrics are observation only (ADR 0075).** Nothing in search, ranking, scoring or the briefing
  reads them, and no agent sees them.
- **A revoked scout keeps its role on the record.** Its past writes still count as scout writes in the
  comparison, and it drops out of `agents.by_role`. A re-activated agent is a scout again until an admin
  clears it.
- **Rolling back migration `0034`** drops the column, so every agent loses its role, and deletes the two
  audit actions' rows (guarded by `--discard-audit-rows`, ADR 0119).
