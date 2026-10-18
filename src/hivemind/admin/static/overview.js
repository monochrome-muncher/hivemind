// Hivemind admin panel: the Overview landing view (ADR 0029).
// Pending queue first, KPI tiles, the activity chart, and short leaderboards.

import {
  api, h, state, loadAgentsAndFleets, fetchMetrics, fetchActivity, optional, pageHead, refreshButton, windowSelector,
  card, stat, unavailable, fmtNum, fmtPct, fmtAgo, fmtTime, link, memoriesHref, feedbackHref, hooks, miniBar,
} from "./core.js";
import { pendingQueue } from "./manage.js";
import { stackedBars } from "./charts.js";
import { openEntry } from "./drawer.js";
import { entryRow } from "./memories.js";

const WRITES = [{ key: "writes", label: "Writes", cls: "s-writes" }];
const VERDICTS = [
  { key: "helpful", label: "Helpful", cls: "s-helpful" },
  { key: "stale", label: "Stale", cls: "s-stale" },
  { key: "wrong", label: "Wrong", cls: "s-wrong" },
];

function activityCard(activity) {
  if (!activity) return card("Activity", unavailable("Activity"));
  return card(`Activity, last ${activity.days} days`, h("div", { class: "charts" },
    stackedBars({ daily: activity.daily, series: WRITES, title: "Writes per day" }),
    stackedBars({ daily: activity.daily, series: VERDICTS, title: "Verdicts per day" })),
  { head: [h("span", { class: "muted small", text: `${fmtNum(activity.totals.writers)} ${activity.totals.writers === 1 ? "writer" : "writers"} · ${fmtNum(activity.totals.reporters)} ${activity.totals.reporters === 1 ? "reporter" : "reporters"}` }),
    link("Who rated what", feedbackHref(), { class: "link small" })] });
}

function topAgents(activity) {
  if (!activity) return card("Most active agents", unavailable("Activity"));
  const top = [...activity.agents].filter((a) => a.recent_writes > 0).sort((a, b) => b.recent_writes - a.recent_writes).slice(0, 5);
  const max = Math.max(1, ...top.map((a) => a.recent_writes));
  return card("Most active agents", top.length
    ? h("div", { class: "bars" }, top.map((a) => h("div", { class: "bar-row bar-row-wide" },
      link(a.name, memoriesHref({ author: a.name })), miniBar(a.recent_writes, max), h("span", { class: "bar-count", text: a.recent_writes }))))
    : h("p", { class: "muted", text: `No writes in the last ${activity.days} days.` }),
  { head: [link("All agents", "#/stats", { class: "link small" })] });
}

function newest(entries) {
  if (!entries) return card("Newest memories", unavailable("The memory list"));
  const rows = entries.map((e) => entryRow({
    id: e.id, kind: e.kind, summary: e.summary, tags: e.tags || [], scope: e.scope, fleet_id: e.fleet_id, author: e.author,
    agent: e.agent, when: e.created_at, state: e.state, feedback: null,
  }, { onOpen: (id) => openEntry(id, () => hooks.refresh()), compact: true }));
  return card("Newest memories", h("div", { class: "table-wrap" }, h("table", { class: "entries" },
    h("thead", {}, h("tr", {}, ["Memory", "Kind", "Scope", "Author", "Written", "State"].map((t) => h("th", { text: t })))),
    h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { class: "empty", colspan: 6, text: "No memories yet." }))))),
  { head: [link("Browse all", memoriesHref(), { class: "link small" })], flush: true });
}

function recentActions(rows) {
  if (!rows) return card("Recent admin actions", unavailable("The audit log"));
  return card("Recent admin actions", rows.length
    ? h("ul", { class: "feed" }, rows.map((r) => h("li", {},
      h("span", { class: "feed-main" }, h("strong", { text: r.action }), r.target ? h("span", { class: "muted", text: ` ${r.target}` }) : null),
      h("span", { class: "muted small", title: fmtTime(r.occurred_at), text: ` ${r.actor} · ${fmtAgo(r.occurred_at)}` }))))
    : h("p", { class: "muted", text: "Nothing yet." }),
  { head: [link("Audit log", "#/audit", { class: "link small" })] });
}

export async function viewOverview(root) {
  const [, metrics, activity, entries, audit] = await Promise.all([
    loadAgentsAndFleets(),
    fetchMetrics(),
    fetchActivity(state.days),
    optional(() => api("GET", "admin/entries", { query: { limit: 8 } })),
    optional(() => api("GET", "admin/audit-log", { query: { limit: 6 } })),
  ]);
  state.metrics = metrics;
  const pending = state.agents.filter((a) => a.status === "pending").length;
  const active = state.agents.filter((a) => a.status === "active").length;
  const searches = metrics && metrics.searches;

  const queue = pending > 0
    ? pendingQueue(() => hooks.refresh())
    : h("p", { class: "quiet-line" }, h("span", { class: "dot dot-ok", "aria-hidden": "true" }), " Nobody is waiting for activation.");

  root.replaceChildren(
    pageHead("Overview", "Registrations waiting on you, and how the agents are using the pool.",
      windowSelector(() => hooks.refresh()), refreshButton()),
    queue,
    h("div", { class: "stats" },
      stat("Pending agents", fmtNum(pending), { warn: pending > 0 }),
      stat("Active agents", fmtNum(active)),
      stat("Fleets", fmtNum(state.fleets.length)),
      stat("Active entries", metrics ? fmtNum(metrics.entries.active) : "—"),
      stat(`Writes, ${state.days}d`, activity ? fmtNum(activity.totals.writes) : "—"),
      stat(`Verdicts, ${state.days}d`, activity ? fmtNum(activity.totals.verdicts) : "—"),
      searches && searches.total ? stat("Searches finding nothing", fmtPct(searches.empty / searches.total), { hint: `${fmtNum(searches.empty)} of ${fmtNum(searches.total)}, all time` }) : null),
    activityCard(activity),
    h("div", { class: "grid-2" }, topAgents(activity), recentActions(audit)),
    newest(entries));
}
