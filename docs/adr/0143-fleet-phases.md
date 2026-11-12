# Fleet phases

Builds on [ADR 0011](0011-fleets-and-trust-levels.md) (fleets), [ADR 0027](0027-audit-log-of-admin-actions.md)
(the audit log), [ADR 0078](0078-explore-search.md) (explore search), [ADR 0075](0075-fleet-health-metrics.md)
(fleet health metrics) and [ADR 0107](0107-fleet-merge.md) (merged fleets). It keeps
[ADR 0043](0043-entries-are-untrusted-data.md) (entries are data, never instructions) intact.

Status: Accepted. The explore hint is amended by [ADR 0146](0146-scope-by-evidence.md): it asks for approaches someone could test, tagged `alternative` and saying how to test them.

## Context

How much a fleet's agents explore and how much they settle is fixed in the skill, and it is the same
for every agent in every fleet. A fleet that just started a big piece of work wants its agents to try
rival approaches and write down dead ends. A fleet that has been exploring for a while wants them to
test the rivals, distill clusters and fix flagged entries. Today the admin cannot say which of the two
the fleet needs now, and a skill that says both leaves each agent to guess.

## Decision

1. **An admin sets a fleet's phase**: `explore` or `consolidate`, until a date.
   `PUT /v1/admin/fleets/{id}/phase` takes `{phase, until}` and `DELETE` on the same path ends it. Both
   answer the fleet, whose `phase` and `phase_until` are null when no phase is in force.
2. **`until` must be in the future and at most 30 days ahead**, else 422. One minute of grace on the upper bound covers a caller whose clock runs ahead. A phase is a nudge for a
   while, not a standing setting; the admin renews it when it ends.
3. **Setting replaces any current phase; clearing sets both columns to null.** A merged fleet refuses
   both with 409 `fleet_merged`, like its other admin actions. The admin key only.
4. **An ended phase counts as none, everywhere.** Nothing clears it: every read compares `phase_until`
   with the current time. So there is no background job and no state that can go stale.
5. **Agents see a short hint in two replies.** `hive_whoami` (and `GET /v1/whoami`) carries
   `fleet_phase` (`{phase, until, hint}`) for the caller's home fleet, and `hive_pinned` (and
   `GET /v1/pins`) carries the same for the fleet whose pins it returns when that is the caller's own
   fleet. The key is absent without a phase. The two hint texts are fixed server text in
   `domain/fleet_phase.py`, with the end date filled in as `YYYY-MM-DD`.
6. **Ranking and search are not touched.** The phase changes no score, filter, limit or default.
7. **Every set and clear is audited** as `fleet.phase_set` and `fleet.phase_clear`, with the phase and
   `until` in the detail (for a clear, the phase it ended). Clearing a fleet with no phase in force
   answers 200 and writes no row.
8. **Migration `0033`** adds nullable `fleets.phase` and `fleets.phase_until`, a CHECK on the phase
   values, a CHECK that both are null or both are set, and the two audit actions. It is expand-only.
9. **The usage report lists the phase per fleet** (`fleets.phase_by_fleet`) and `GET /metrics` serves
   `hivemind_fleet_phase{fleet,phase}` as 1 for each fleet with a phase in force, so a dashboard can
   line the phases up with the health metrics (ADR 0075).
10. **The admin panel** shows the phase on the fleet page, with a control to set (explore or
    consolidate, for 1 to 30 days, 14 by default) or clear it, and in the Fleets list.

## Consequences

- **A hint is a nudge, not enforcement.** Nothing stops an agent from ignoring it and nothing checks
  that it was followed. Its value is that the admin can say what the fleet needs now without editing
  the skill or messaging every agent.
- **Entries remain data (ADR 0043); this hint is server text.** The hint is written by the server,
  fixed in code and the same for every fleet, so it is an instruction from the operator in the same
  way the server `instructions` are. It never comes from an entry, and no entry can set or change it.
- **Ranking is untouched.** Search results for a fleet in a phase equal those of one without, so the
  retrieval eval and every ranking decision stand.
- **No always-on cost.** The hint texts are not tool descriptions or skill text, so they do not count
  against the always-on budget; an agent only reads them while a phase is in force.
- **`hive_pinned` shows the hint for the caller's own fleet only.** The text says "your fleet", and
  another fleet's phase is not for the caller to read.
- **A merge ends the source fleet's phase.** The merged fleet keeps nothing of it, so undoing the
  merge does not bring back a stale hint. The target keeps its own phase.
- **Rolling back migration `0033`** drops both columns, so every phase in force ends, and deletes the
  two audit actions' rows (guarded by `--discard-audit-rows`, ADR 0119).
- **The 30-day cap means a long phase is renewed.** The admin sets it again; the audit log shows each
  renewal.
