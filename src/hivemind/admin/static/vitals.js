// Hivemind admin panel: the vital signs of the hive (ADR 0142). Pure: no DOM, so a test can run it
// as it ships. Observation only: it reads the numbers of the health report and nothing reads it back.

import { fmtDuration, fmtNum, fmtOf } from "./ui.js";

// Starting points, to be tuned against your own hive. The shipped Prometheus alerts
// (deploy/kubernetes/optional/monitoring/alerts.yaml) use the echo rate and the
// alternative age below: change them together.
export const VITAL_THRESHOLDS = {
  echoRate: 0.2,
  dispersionChange: 0.05,
  groundingShare: 0.4,
  alternativeAgeDays: 60,
  conflictAgeDays: 30,
  contestedAgeDays: 30,
  flaggedBacklog: 0,
};

export const STEADY = "Steady";
export const WATCH = "Watch";

const SECONDS_PER_DAY = 86400;
const stateOf = (watching) => (watching ? WATCH : STEADY);
const worst = (items) => stateOf(items.some((item) => item.state === WATCH));
const signed = (x) => `${x > 0 ? "+" : ""}${Math.round(x * 1000) / 1000}`;

const signal = (label, value, watching) => ({ label, value, state: stateOf(watching) });

/** A rival kind's count and median age; Watch when the median age is over `days`. */
function rivalSignal(label, rival, days) {
  const value = rival.count ? `${rival.count} · ${fmtDuration(rival.median_age_seconds)}` : "—";
  return signal(label, value, rival.median_age_seconds != null && rival.median_age_seconds > days * SECONDS_PER_DAY);
}

function collapsing(health, activeEntries, t) {
  const rising = health.topic_dispersion.filter((d) => d.change > t.dispersionChange);
  const alternatives = health.rival_lifetime.alternative.count;
  const grounding = health.grounding_share;
  const signals = [
    signal("Echo rate", fmtOf(health.echo_rate), health.echo_rate.share != null && health.echo_rate.share > t.echoRate),
    signal("Busy tags converging", rising.length ? rising.map((d) => `${d.tag} ${signed(d.change)}`).join(", ") : "none", rising.length > 0),
    signal("Open alternatives", fmtNum(alternatives), alternatives === 0 && activeEntries > 0),
    signal("Grounding share", fmtOf(grounding), grounding.share != null && grounding.share < t.groundingShare),
  ];
  return { state: worst(signals), signals };
}

function scattering(health, t) {
  const rivals = health.rival_lifetime;
  const signals = [
    rivalSignal("Open alternatives, median age", rivals.alternative, t.alternativeAgeDays),
    rivalSignal("Open Conflict entries, median age", rivals.conflict, t.conflictAgeDays),
    rivalSignal("Contested entries, median age", rivals.contested, t.contestedAgeDays),
    signal("Flagged more than 14 days ago", fmtNum(health.flagged_backlog_14d), health.flagged_backlog_14d > t.flaggedBacklog),
  ];
  return { state: worst(signals), signals };
}

/** One row per fleet; a fleet with active entries but no writes and no verdicts in 7 days is quiet. */
function breathing(health) {
  const fleets = Object.entries(health.by_fleet)
    .map(([name, f]) => ({ name, ...f.breathing_7d, state: stateOf(f.active_entries > 0 && f.breathing_7d.writes === 0 && f.breathing_7d.verdicts === 0) }))
    .sort((a, b) => a.name.localeCompare(b.name));
  return { state: worst(fleets), fleets };
}

/** The three bands from the usage report; `metrics.health` must be present. */
export function vitalSigns(metrics, thresholds = VITAL_THRESHOLDS) {
  const { health } = metrics;
  return {
    collapsing: collapsing(health, metrics.entries.active, thresholds),
    scattering: scattering(health, thresholds),
    breathing: breathing(health),
  };
}
