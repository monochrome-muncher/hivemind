// Hivemind admin panel: fleet health (ADR 0075). Observation only, for the
// operator: nothing agent-facing reads these numbers.

import {
  api, h, state, guarded, parseHash, viewHref, fleetName, agentHref, fmtOf, fmtDuration, table, card, pageHead, refreshButton, fetchMetrics,
  loadAgentsAndFleets, unavailable, fill, link,
} from "./core.js";
import { openEntry } from "./drawer.js";
import { flaggedTable } from "./lists.js";

// The flagged list's filters (ADR 0093), as #/health?flags=old|orphaned&fleet=<id>.
const FLAG_FILTERS = [
  { value: "", text: "All", query: {} },
  { value: "old", text: "Flagged over 14 days", query: { older_than_days: 14 } },
  { value: "orphaned", text: "No owner", query: { orphaned: true } },
];
const flagsHref = (flags) => viewHref("health", { flags });
const FLAGGED_ID = "flagged-memories";
// A count linking to its list. Already on that hash, a click changes nothing,
// so it scrolls to the list itself.
const flagsLink = (count, flags) => {
  const href = flagsHref(flags);
  return link(String(count), href, { title: "List them", onclick: (event) => {
    if (window.location.hash !== href) return;
    event.preventDefault();
    const list = document.getElementById(FLAGGED_ID);
    if (list) list.scrollIntoView({ block: "start" });
  } });
};

const fmtRival = (r) => (r.count ? `${r.count} · ${fmtDuration(r.median_age_seconds)}` : "0");

function dispersionTable(rows) {
  const cos = (x) => Math.round(x * 1000) / 1000;
  return h("div", {},
    h("div", { class: "card-body" }, h("p", { class: "muted small", text: "Topic dispersion: mean pairwise cosine of a busy tag's entries, last 30 days against the 30 before (newest 50 per window). A rise means writers are converging." })),
    table(["Tag", "Last 30 days", "Before", "Change"], rows.map((d) => h("tr", {},
      h("td", { text: d.tag }), h("td", { text: `${cos(d.recent_cosine)} · ${d.recent_entries} entries` }),
      h("td", { text: `${cos(d.before_cosine)} · ${d.before_entries} entries` }),
      h("td", { text: `${d.change > 0 ? "+" : ""}${cos(d.change)}` }))), "No tag has 5 entries in both windows yet."));
}

// ADR 0075: the fleet health metrics. Observation only for the operator; nothing
// agent-facing reads them, so no target should be set for an agent.
export function healthCard(health) {
  if (!health) return unavailable("Fleet health");
  const tags = health.gap_tag_counts;
  const rows = [
    ["Helpful verdicts with a note", fmtOf(health.helpful_with_note_share), "Up: verdicts carry evidence"],
    ["Sourced active org/fleet facts and insights", fmtOf(health.sourced_share), "Up: traceable"],
    ["Correction latency p50 (first flag to successor, 90 days)", `${fmtDuration(health.correction_latency_p50_seconds)} · ${health.corrections_measured} measured`, "Down: errors get fixed faster"],
    ["Flagged more than 14 days ago, still active", flagsLink(health.flagged_backlog_14d, "old"), "Down"],
    ["Flagged entries with no owner (author revoked or silent 30 days)", flagsLink(health.orphaned_flagged, "orphaned"), "Down"],
    ["Floating gap (older than 90 days, no feedback in 90 days)", health.floating_gap, "Down, or stable with growth"],
    ["Top 3 authors' share of writes (90 days)", `${fmtOf(health.top3_author_write_share)} · ${health.top3_author_write_share.authors} authors`, "Not rising"],
    ["Top 3 reporters' share of verdicts (90 days)", `${fmtOf(health.top3_reporter_verdict_share)} · ${health.top3_reporter_verdict_share.reporters} reporters`, "Not rising"],
    ["Verdicts on a sibling agent's entry (same owner alias, different agent; 90 days). Owner aliases are self-reported, so this is a lower bound", fmtOf(health.sibling_verdict_share), "Not rising"],
    ["Top 3 owners' share of verdicts (90 days). Owner aliases are self-reported; an agent without one counts as its own owner", `${fmtOf(health.top3_owner_verdict_share)} · ${health.top3_owner_verdict_share.owners} owners`, "Not rising"],
    ["Insights linking at least 2 entries", fmtOf(health.synthesis_rate), "Up: combining happens"],
    ["Self entries promoted to the fleet (90 days)", health.self_to_fleet_promotions, "Up"],
    ["Supersessions per 100 writes (90 days)", health.supersessions_per_100_writes.per_100 == null ? "—" : Math.round(health.supersessions_per_100_writes.per_100 * 10) / 10, "Up modestly"],
    ["Echo rate: new writes (30 days, newest 200 sampled) with an earlier entry at cosine 0.9 or more", fmtOf(health.echo_rate), "Not rising"],
    ["Grounding: new fleet/org insights and decisions with a source that is not an entry (30 days)", fmtOf(health.grounding_share), "Up"],
    ["Top 3 agent labels' share of writes (30 days)", `${fmtOf(health.top3_agent_label_write_share)} · ${health.top3_agent_label_write_share.labels} labels`, "Not rising"],
    ["Top 3 agent labels' share of verdicts (30 days)", `${fmtOf(health.top3_agent_label_verdict_share)} · ${health.top3_agent_label_verdict_share.labels} labels`, "Not rising"],
    ["Open alternative entries (count · median age)", fmtRival(health.rival_lifetime.alternative), "Zero means early closure; a growing pile means nobody tests"],
    ["Open “Conflict:” entries (count · median age)", fmtRival(health.rival_lifetime.conflict), "Shrinking as they are tested"],
    ["Contested entries, helpful beside stale or wrong (count · median age)", fmtRival(health.rival_lifetime.contested), "Resolved over time"],
    ["Gap tags (anomaly / open-question / dead-end)", `${tags.anomaly} / ${tags["open-question"]} / ${tags["dead-end"]}`, "Visible; read beside the empty-search rate"],
  ];
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", { text: "Fleet health" })),
    h("div", { class: "card-body" }, h("p", { class: "muted small", text: "For you only: observations of the pool, refreshed with the entry counts. Agents never see these numbers and nothing ranks by them." })),
    table(["Metric", "Now", "Healthy direction"], rows.map(([label, value, hint]) => h("tr", {},
      h("td", { text: label }), h("td", {}, value), h("td", { class: "muted", text: hint }))), ""),
    dispersionTable(health.topic_dispersion));
}

/**
 * The flag backlog (ADR 0093): active memories with a standing stale or
 * wrong verdict, oldest flag first, so the operator can chase the ones
 * nobody fixed. Filters live in the hash so the metric rows can link here.
 */
function flaggedList(root) {
  const { params } = parseHash();
  let flags = FLAG_FILTERS.some((f) => f.value === params.get("flags")) ? params.get("flags") : "";
  const fleet = h("select", { "aria-label": "Fleet" }, h("option", { value: "", text: "All fleets" }),
    state.fleets.map((f) => h("option", { value: f.id, text: f.name, selected: f.id === params.get("fleet") })));
  const group = h("div", { class: "segmented", role: "group", "aria-label": "Which flagged memories" });
  const paint = () => group.replaceChildren(...FLAG_FILTERS.map((f) => h("button", {
    type: "button", class: f.value === flags ? "seg active" : "seg", "aria-pressed": f.value === flags ? "true" : "false", text: f.text,
    onclick: () => { flags = f.value; paint(); list.load(true); },
  })));
  paint();
  const syncHash = () => {
    const next = viewHref("health", { flags, fleet: fleet.value });
    if (root.dataset.superseded) return; // the user navigated away while this view loaded
    if (window.location.hash !== next) history.replaceState(null, "", next);
  };
  const list = flaggedTable({
    fetchPage: ({ limit, offset }) => {
      syncHash();
      const { query } = FLAG_FILTERS.find((f) => f.value === flags);
      return guarded(() => api("GET", "admin/flagged", { query: { ...query, fleet_id: fleet.value, limit, offset } }));
    },
    onOpen: (id) => openEntry(id, () => list.load(true)),
    fleetName,
    agentHref,
    csvName: "flagged",
  });
  fleet.addEventListener("change", () => list.load(true));
  const head = h("div", { class: "card", id: FLAGGED_ID },
    h("div", { class: "card-head" }, h("h2", { text: "Flagged memories" })),
    h("div", { class: "card-body" },
      h("p", { class: "muted small", text: "Active memories with a standing stale or wrong verdict, the oldest flag first. Open one to read the verdicts, then withdraw it or leave it for its author or a successor. “No owner” means the author is revoked or has not written for 30 days." }),
      h("div", { class: "filters" }, group, fleet)));
  // Arriving from a metric's count (or a refresh with a filter on): show the list.
  const filtered = Boolean(flags || fleet.value);
  return { els: [head, list.el], load: () => list.load(true), reveal: () => { if (filtered) head.scrollIntoView({ block: "start" }); } };
}

export async function viewHealth(root) {
  const [, metrics] = await Promise.all([loadAgentsAndFleets(), fetchMetrics()]);
  const flagged = flaggedList(root);
  fill(root,
    pageHead("Health", "Observations of the pool, for you only. Nothing ranks by these and no agent sees them.", refreshButton()),
    metrics && metrics.health ? healthCard(metrics.health) : card("Fleet health", unavailable("Fleet health")),
    ...flagged.els);
  await flagged.load();
  setTimeout(flagged.reveal, 0); // after the router puts this view on the page
}
