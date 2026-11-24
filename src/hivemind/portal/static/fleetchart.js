// Hivemind agent portal (ADR 0165): the home page's fleet activity chart.
// One line per agent of the key's home fleet, time on the x axis (UTC days), one y axis from zero.
// Activity only: nothing here ranks agents or reads back into retrieval. Every label is set
// through textContent (h(), svg()).

import { h, svg, card, table, unavailable } from "./ui.js";
import { niceMax } from "./charts.js";

export const MAX_LINES = 7;
export const OTHER_NAME = "Other agents";
export const METRICS = [["writes", "Writes"], ["ratings", "Ratings"]];
export const WINDOWS = [7, 30];

const END_LABEL_MAX_SERIES = 4;
const W = 720;
const H = 220;
const TOP = 10;
const BOTTOM = 22;
const LEFT = 34;
const END_LABEL_ROOM = 104;
const MARKER_MIN_PX = 4.5;
const LABEL_GAP = 12;

const byName = (a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
const total = (agent) => agent.writes + agent.ratings;

/**
 * The lines to draw for `metric`. The agents shown are the busiest over the window (writes and
 * ratings together), so flipping the metric never changes who is shown; they take the colour
 * slots 1..7 in name order, so an agent keeps its colour. The rest fold into one "Other agents"
 * line.
 */
export function chartSeries(agents, metric) {
  const shown = new Set([...agents].sort((a, b) => total(b) - total(a) || byName(a, b)).slice(0, MAX_LINES).map((a) => a.name));
  const named = [...agents].filter((a) => shown.has(a.name)).sort(byName);
  const series = named.map((agent, i) => ({ name: agent.name, slot: String(i + 1), values: agent.daily.map((d) => d[metric]) }));
  const rest = agents.filter((a) => !shown.has(a.name));
  if (rest.length) {
    const values = rest[0].daily.map((_, i) => rest.reduce((sum, a) => sum + a.daily[i][metric], 0));
    series.push({ name: OTHER_NAME, slot: "other", values });
  }
  return series;
}

/** Push end labels apart so no two overlap, keeping their order and staying inside the plot. */
export function spreadLabels(ys, gap, min, max) {
  const order = ys.map((y, i) => [y, i]).sort((a, b) => a[0] - b[0]);
  const placed = [];
  for (const [y] of order) placed.push(Math.max(y, placed.length ? placed[placed.length - 1] + gap : min));
  for (let i = placed.length - 1; i >= 0; i--) placed[i] = Math.min(placed[i], i === placed.length - 1 ? max : placed[i + 1] - gap);
  const out = [];
  order.forEach(([, original], i) => { out[original] = placed[i]; });
  return out;
}

const shortDay = (day) => day.slice(5); // MM-DD
const clip = (name) => (name.length > 14 ? `${name.slice(0, 13)}…` : name);

/** The SVG line chart of `series` over `days` (ISO dates), with a crosshair readout and a legend. */
export function lineChart({ days, series, title }) {
  const direct = series.length <= END_LABEL_MAX_SERIES;
  const right = direct ? END_LABEL_ROOM : 8;
  const innerW = W - LEFT - right;
  const innerH = H - TOP - BOTTOM;
  const max = niceMax(Math.max(0, ...series.flatMap((s) => s.values)));
  const step = days.length > 1 ? innerW / (days.length - 1) : 0;
  const x = (i) => (days.length > 1 ? LEFT + i * step : LEFT + innerW / 2);
  const y = (v) => TOP + innerH - (v / max) * innerH;

  const summary = `${title}, ${days[0]} to ${days[days.length - 1]}: ${series.map((s) => `${s.name} ${s.values.reduce((a, b) => a + b, 0)}`).join(", ")}.`;
  const readout = h("p", { class: "chart-readout muted small", "aria-live": "polite", text: summary });
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "group", "aria-label": summary, preserveAspectRatio: "xMidYMid meet" });

  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i;
    root.append(
      svg("line", { x1: LEFT, x2: W - right, y1: y(v), y2: y(v), class: i === 0 ? "chart-axis" : "chart-grid" }),
      svg("text", { x: LEFT - 6, y: y(v) + 4, class: "chart-label chart-label-y", text: Number.isInteger(v) ? String(v) : v.toFixed(1) }));
  }
  const every = Math.max(1, Math.ceil(days.length / 6));
  days.forEach((day, i) => {
    if (i % every !== 0 && i !== days.length - 1) return;
    if (i !== days.length - 1 && days.length - 1 - i < every / 2) return;
    root.append(svg("text", { x: x(i), y: H - 6, class: "chart-label chart-label-x", text: shortDay(day) }));
  });

  for (const s of series) {
    const points = s.values.map((v, i) => `${x(i)},${y(v)}`).join(" ");
    root.append(svg("polyline", { points, class: `line-series series-${s.slot}`, fill: "none" }));
  }
  if (direct) {
    const ys = spreadLabels(series.map((s) => y(s.values[s.values.length - 1])), LABEL_GAP, TOP + 4, H - BOTTOM);
    series.forEach((s, i) => root.append(svg("text", { x: W - right + 6, y: ys[i] + 4, class: "chart-label chart-label-end", text: clip(s.name) })));
  }

  const hover = svg("g", { class: "chart-hover" });
  const hit = svg("rect", { x: LEFT, y: TOP, width: innerW, height: innerH, class: "chart-hit", tabindex: "0", role: "application", "aria-label": `${title}: use the arrow keys to read each day` });
  // One day has no line to see: its points are drawn.
  if (days.length === 1) series.forEach((s) => root.append(svg("circle", { cx: x(0), cy: y(s.values[0]), r: MARKER_MIN_PX, class: `chart-marker series-${s.slot}` })));
  root.append(hover, hit);

  let at = null;
  const show = (i) => {
    const next = Math.max(0, Math.min(days.length - 1, i));
    if (next === at) return;
    at = next;
    const rect = root.getBoundingClientRect();
    const scale = rect.width ? rect.width / W : 1;
    const radius = Math.max(MARKER_MIN_PX, MARKER_MIN_PX / scale);
    hover.replaceChildren(
      svg("line", { x1: x(at), x2: x(at), y1: TOP, y2: H - BOTTOM, class: "chart-cross" }),
      ...series.map((s) => svg("circle", { cx: x(at), cy: y(s.values[at]), r: radius, class: `chart-marker series-${s.slot}` })));
    readout.textContent = `${days[at]}: ${series.map((s) => `${s.name} ${s.values[at]}`).join(", ")}`;
  };
  const clear = () => { at = null; hover.replaceChildren(); readout.textContent = summary; };
  const nearest = (event) => {
    const rect = root.getBoundingClientRect();
    const px = ((event.clientX - rect.left) * W) / (rect.width || W);
    return step ? Math.round((px - LEFT) / step) : 0;
  };
  hit.addEventListener("mousemove", (event) => show(nearest(event)));
  hit.addEventListener("mouseleave", clear);
  hit.addEventListener("focus", () => show(days.length - 1));
  hit.addEventListener("blur", clear);
  hit.addEventListener("keydown", (event) => {
    const moves = { ArrowLeft: (at ?? days.length) - 1, ArrowRight: (at ?? -1) + 1, Home: 0, End: days.length - 1 };
    if (!(event.key in moves)) return;
    event.preventDefault();
    show(moves[event.key]);
  });

  const legend = series.length >= 2
    ? h("div", { class: "legend" }, series.map((s) => h("span", { class: "legend-item" }, h("span", { class: `swatch series-${s.slot}`, "aria-hidden": "true" }), s.name)))
    : null;
  return h("figure", { class: "chart-figure" }, root, legend, readout);
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
    const note = h("p", { class: "muted small note", text: quietNote(report.quiet) + COUNTING_NOTE });
    if (report.agents.length === 0) {
      body.replaceChildren(h("p", { class: "muted", text: `No writes or ratings in this fleet in the last ${report.days} days.` }), note);
      return;
    }
    const series = chartSeries(report.agents, metric);
    const dayList = report.agents[0].daily.map((d) => d.day);
    const label = METRICS.find(([key]) => key === metric)[1];
    body.replaceChildren(lineChart({ days: dayList, series, title: `${label} per day` }), asTable(dayList, series), note);
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
  return { el: card("Activity per agent", body, { head: [h("div", { class: "row tight" }, metricToggle, windowToggle)] }), load };
}
