# Vital signs for the hive

Extends [ADR 0075](0075-fleet-health-metrics.md) (fleet health metrics) and builds on
[ADR 0050](0050-prometheus-scrape-endpoint.md) (the scrape endpoint), [ADR 0067](0067-a-revoked-agents-feedback-no-longer-counts.md)
(revoked reporters do not count) and [ADR 0069](0069-cache-the-entry-scan-behind-the-usage-report.md)
(the cached scan). The idea comes from section 3 of
[the living hive proposals](../proposals/2026-11-12-living-hive-ideas.md).

Status: Accepted.

## Context

ADR 0075 gave the admin sensors for the pool: echo rate, topic dispersion, rival lifetime, grounding,
the flag backlog and more. They sit in one long table and in separate Grafana panels, and nothing
tells the admin when one of them has gone somewhere worth a look. Two things are also missing.

* There is no sign of whether the hive is alive. A fleet whose agents stopped writing and rating looks
  healthy in every ratio, because ratios over nothing stay where they were.
* The numbers do not say what to do. A rising echo rate and a pile of old rivals call for opposite
  responses: ask agents to explore, or ask them to settle.

The same Goodhart rule as ADR 0075 holds: the moment an agent can see these numbers, they stop
measuring anything.

## Decision

1. **Breathing counts.** The health scan also counts four things over the 7 days before the report's
   `now` (168 hours, like the other windows, so the server TimeZone does not matter), per fleet and,
   by summing, for the whole pool:

   | Count | Meaning |
   |---|---|
   | `writes` | entries created, in any state |
   | `verdicts` | verdicts whose `updated_at` is in the window, on the fleet's entries; revoked reporters are left out (ADR 0067) |
   | `supersessions` | entries superseded by a successor created in the window (the definition of `supersessions_per_100_writes`, counted for the fleet of the superseded entry) |
   | `withdrawals` | entries whose `withdrawn_at` is in the window |

   The fleet is always the `fleet_id` of the entry concerned, so entries with no fleet count in the
   pool-wide numbers only. They come from the existing `Store.fleet_health` read, with no new method
   and no migration: `HealthCounts` gains `writes_7d`, `verdicts_7d`, `supersessions_7d`,
   `withdrawals_7d` and `active_entries` (the fleet's active entries, which the quiet-fleet rule
   needs). The scan runs one more statement in the same snapshot, over the entries created or
   withdrawn in the window, the verdicts of the window and the supersessions of the window.

2. **Where they are served.** `GET /v1/metrics` adds `health.breathing_7d` and
   `health.by_fleet[<name>].breathing_7d`, each `{writes, verdicts, supersessions, withdrawals}`, and
   `health.by_fleet[<name>].active_entries`. Prometheus gets `hivemind_health_breathing_7d{motion}`,
   `hivemind_health_fleet_breathing_7d{fleet,motion}` and `hivemind_health_fleet_active_entries{fleet}`,
   with `motion` one of `writes`, `verdicts`, `supersessions`, `withdrawals`. The echo rate, grounding
   share, rival counts and median ages, and the floating gap already have gauges (ADR 0075); no other
   gauge was needed.

3. **A "Vital signs" card** on the admin Health page, above "Fleet health". It groups existing numbers
   into three bands. A signal is Steady or Watch, and a band is Watch when any of its signals is. A
   number with no denominator shows as a dash and counts as Steady.

   | Band | Signals (Watch when) | Lean towards |
   |---|---|---|
   | Collapsing: members converging on one view | echo rate above 0.20; any busy tag's dispersion change above +0.05; no open `alternative` entry while the pool has entries; grounding share below 0.40 | exploring: ask agents for alternatives and dead ends, and to use explore search on open-ended work |
   | Scattering: disagreements piling up untested | median age of open alternatives above 60 days; of open `Conflict:` entries above 30 days; of contested entries above 30 days; any entry in the 14-day flagged backlog | settling: test rivals and conflicts, fix flagged entries, distill clusters |
   | Breathing: is the hive alive | a table of the 7-day counts per fleet; a fleet is Watch when it has active entries and had no writes and no verdicts | checking that the fleet's agents are connected and rating what they use |

   The thresholds are starting points. They live in one constant, `VITAL_THRESHOLDS`, in
   `admin/static/vitals.js`, a module with no DOM so a test can run it as it ships (like `needs.js`,
   ADR 0121). `health.js` only draws what it returns.

4. **Optional alerts.** `deploy/kubernetes/optional/monitoring/alerts.yaml` is a Prometheus Operator
   `PrometheusRule` named `hivemind-vital-signs`, in the same opt-in tree as the ServiceMonitors and
   the dashboard. All four alerts have severity `info`, and each annotation says what the signal
   means and what the admin could do:

   | Alert | Fires when | For |
   |---|---|---|
   | `HivemindFleetQuiet` | a fleet with active entries had 0 writes and 0 verdicts in 7 days | 1 hour |
   | `HivemindEchoRising` | echo ratio above 0.2 | 1 day |
   | `HivemindRivalsLingering` | median age of open alternatives above 60 days | 1 day |
   | `HivemindFloatingGapGrowing` | the floating gap is higher than it was 28 days ago | 1 day |

   The echo and rival thresholds are the ones in `VITAL_THRESHOLDS`; a comment in both places says to
   change them together, and a test compares them. The dashboard gets a matching "Vital signs" row.

5. **Observation only, as before.** The card and the alerts are for the admin. No agent-facing surface
   returns these numbers, and nothing ranks, scores or briefs by them (ADR 0075, decision 2). The
   server never switches an agent's behaviour on a band: the admin reads it and decides.

## Consequences

- A fleet that goes silent now shows up as Watch and, with the alerts applied, as an `info` alert
  after an hour, instead of staying invisible behind unchanged ratios.
- The usage report grows by one object per fleet and one for the pool. The scan costs one more pass
  over the entries of the last 7 days plus the verdicts of the window; it is cached with the rest of
  the health read (ADR 0075, decision 8).
- `by_fleet` can now list a fleet that has no active entries but did have activity in the window, such
  as one whose entries were all withdrawn this week.
- `withdrawals` rests on `withdrawn_at` (migration 0016), which that migration set to the migration
  time for withdrawals older than the column. Right after upgrading, those older withdrawals count
  as this week's for 7 days. The ADR 0075 remark that entries carry no withdrawal time is out of date;
  withdrawing within an hour of the write is still not measured.
- The thresholds are guesses. They will be tuned against real hives, in `vitals.js` and
  `alerts.yaml` together. A band in Watch is a reason to look, not a fault.
- Showing any of this to agents, or letting a component act on a band by itself, needs a new ADR that
  supersedes ADR 0075.
