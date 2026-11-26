// Hivemind agent portal (ADRs 0165, 0167): the home page's fleet activity card.
// Daily bars stacked by agent, so a bar's height is the fleet's day and its colours are who
// did it; under it, each agent's total in name order and, for ratings, the fleet's verdict mix.
// Activity only: nothing here ranks agents or reads back into retrieval. Every label is set
// through textContent (h()).

import { h, card, table, unavailable, miniBar } from "./ui.js";
import { stackedBars } from "./charts.js";

export const MAX_SERIES = 7;
export const OTHER_NAME = "Other agents";
export const METRICS = [["writes", "Writes"], ["ratings", "Ratings"]];
export const WINDOWS = [7, 30];
export const VERDICTS = [["helpful", "Helpful"], ["stale", "Stale"], ["wrong", "Wrong"]];

const byName = (a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
const total = (agent) => agent.writes + agent.ratings;

/**
 * The stacks to draw for `metric`. The agents shown are the busiest over the window (writes and
 * ratings together), so flipping the metric never changes who is shown; they take the colour
 * slots 1..7 in name order, so an agent keeps its colour. The rest fold into one "Other agents"
 * stack.
 */
export function chartSeries(agents, metric) {
  const shown = new Set([...agents].sort((a, b) => total(b) - total(a) || byName(a, b)).slice(0, MAX_SERIES).map((a) => a.name));
  const named = [...agents].filter((a) => shown.has(a.name)).sort(byName);
  const series = named.map((agent, i) => ({ name: agent.name, slot: String(i + 1), values: agent.daily.map((d) => d[metric]) }));
  const rest = agents.filter((a) => !shown.has(a.name));
  if (rest.length) {
    const values = rest[0].daily.map((_, i) => rest.reduce((sum, a) => sum + a.daily[i][metric], 0));
    series.push({ name: OTHER_NAME, slot: "other", values, members: rest.map((a) => a.name) });
  }
  return series;
}

/** The fleet's verdicts over the window: each verdict's count and its whole-number share. */
export function verdictSplit(verdicts) {
  const counts = VERDICTS.map(([key, label]) => ({ key, label, count: verdicts[key] }));
  const all = counts.reduce((sum, c) => sum + c.count, 0);
  return { total: all, parts: counts.map((c) => ({ ...c, share: all ? Math.round((c.count / all) * 100) : 0 })) };
}

function bars(days, series, title) {
  const daily = days.map((day, i) => Object.fromEntries([["day", day], ...series.map((s) => [s.slot, s.values[i]])]));
  return stackedBars({ daily, series: series.map((s) => ({ key: s.slot, label: s.name, cls: `series-${s.slot}` })), title, showTitle: false });
}

function agentTotals(agents, series, metric) {
  const slotOf = new Map(series.filter((s) => s.slot !== "other").map((s) => [s.name, s.slot]));
  const max = Math.max(1, ...agents.map((a) => a[metric]));
  const rows = [...agents].sort(byName).map((a) => h("div", { class: "agent-total" },
    h("span", { class: "agent-total-name" }, h("span", { class: `swatch series-${slotOf.get(a.name) || "other"}`, "aria-hidden": "true" }), a.name),
    miniBar(a[metric], max, `series-fill-${slotOf.get(a.name) || "other"}`),
    h("span", { class: "num", text: a[metric].toLocaleString() })));
  return h("section", { class: "activity-part" }, h("h3", { text: "By agent" }), h("div", { class: "agent-totals" }, rows));
}

function verdictMix(verdicts) {
  const { total: all, parts } = verdictSplit(verdicts);
  if (!all) return null;
  return h("section", { class: "activity-part" },
    h("h3", { text: "Verdicts across the fleet" }),
    h("div", { class: "split-bar", role: "img", "aria-label": parts.map((p) => `${p.label} ${p.count} (${p.share}%)`).join(", ") },
      parts.filter((p) => p.count).map((p) => {
        const seg = h("span", { class: `split-seg s-${p.key}` });
        seg.style.flexGrow = String(p.count);
        return seg;
      })),
    h("div", { class: "legend" }, parts.map((p) => h("span", { class: "legend-item" },
      h("span", { class: `swatch s-${p.key}`, "aria-hidden": "true" }), p.label,
      h("span", { class: "legend-total", text: p.count.toLocaleString() }), h("span", { text: `${p.share}%` })))));
}

const asTable = (days, series) => h("details", { class: "chart-table" },
  h("summary", { text: "Show as table" }),
  table(["Day", ...series.map((s) => s.name)],
    days.map((day, i) => h("tr", {}, h("td", { text: day }), series.map((s) => h("td", { class: "num", text: s.values[i] })))),
    "No days."));

const quietNote = (n) => (n > 0 ? `${n} more active ${n === 1 ? "agent" : "agents"} had no writes or ratings in this window. ` : "");
const COUNTING_NOTE = "Writes and ratings count only memories this key can read. A rating counts on the day it was last given.";

function segmented(label, options, get, set) {
  const group = h("div", { class: "segmented", role: "group", "aria-label": label });
  const paint = () => group.replaceChildren(...options.map(([value, text]) => h("button", {
    type: "button", class: value === get() ? "seg active" : "seg", "aria-pressed": value === get() ? "true" : "false", text,
    onclick: () => { set(value); paint(); },
  })));
  paint();
  return group;
}

/**
 * The card for the home page. `fetchReport(days)` resolves to the report of
 * `GET /v1/portal/fleet-activity` and rejects when it cannot be read (a 404 means an API from
 * before the route). `canRead` is false for a key that can read no memories.
 */
export function fleetActivityCard(fetchReport, { canRead = true } = {}) {
  let metric = "writes";
  let days = 30;
  let report = null;
  let failure = null;
  let token = 0;
  const body = h("div", {});

  const paint = () => {
    if (!canRead) { body.replaceChildren(h("p", { class: "muted", text: "This key can read no memories, so there is no activity to show." })); return; }
    if (failure) {
      body.replaceChildren(failure.status === 404 ? unavailable("Fleet activity") : h("p", { class: "muted", text: "Could not load fleet activity." }));
      return;
    }
    if (!report) return; // still loading
    const note = h("p", { class: "muted small note", text: quietNote(report.quiet) + COUNTING_NOTE });
    if (report.agents.length === 0) {
      body.replaceChildren(h("p", { class: "muted", text: `No writes or ratings in this fleet in the last ${report.days} days.` }), note);
      return;
    }
    const series = chartSeries(report.agents, metric);
    const dayList = report.agents[0].daily.map((d) => d.day);
    const label = METRICS.find(([key]) => key === metric)[1];
    body.replaceChildren(
      bars(dayList, series, `${label} per day`),
      h("div", { class: "activity-parts" }, agentTotals(report.agents, series, metric), metric === "ratings" && report.verdicts ? verdictMix(report.verdicts) : null),
      asTable(dayList, series), note);
  };
  const load = async () => {
    const mine = ++token;
    if (!canRead) { paint(); return; }
    let fetched = null;
    let error = null;
    try { fetched = await fetchReport(days); } catch (err) { error = err; }
    if (mine !== token) return;
    report = fetched;
    failure = error;
    paint();
  };
  const metricToggle = segmented("Measure", METRICS, () => metric, (value) => { metric = value; paint(); });
  const windowToggle = segmented("Time window", WINDOWS.map((d) => [d, `${d} days`]), () => days, (value) => { days = value; load(); });
  return { el: card("Fleet activity", body, { head: [metricToggle, windowToggle] }), load };
}
