// Hivemind front-end kit: the SVG activity chart (ADR 0029).
// Built with createElementNS; every label is set through textContent.

import { h, svg } from "./ui.js";

const W = 720;
const H = 190;
const M = { left: 34, right: 6, top: 8, bottom: 22 };

function niceMax(value) {
  if (value <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) if (step * pow >= value) return step * pow;
  return 10 * pow;
}

const shortDay = (day) => day.slice(5); // MM-DD

/**
 * A stacked daily bar chart.
 * `daily` is [{day, ...counts}], `series` is [{key, label, cls}] (bottom first).
 * Returns a figure with the chart, a legend and a hover/focus readout.
 */
export function stackedBars({ daily, series, title }) {
  const innerW = W - M.left - M.right;
  const innerH = H - M.top - M.bottom;
  const totals = daily.map((d) => series.reduce((sum, s) => sum + (d[s.key] || 0), 0));
  const max = niceMax(Math.max(0, ...totals));
  const band = daily.length ? innerW / daily.length : innerW;
  const barW = Math.max(1.5, band * 0.68);
  const y = (v) => M.top + innerH - (v / max) * innerH;

  const grand = series.map((s) => daily.reduce((sum, d) => sum + (d[s.key] || 0), 0));
  const summary = `${title}: ${series.map((s, i) => `${grand[i]} ${s.label.toLowerCase()}`).join(", ")} over ${daily.length} days.`;
  const readout = h("p", { class: "chart-readout muted small", "aria-live": "polite", text: summary });

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": summary, preserveAspectRatio: "xMidYMid meet" });
  // Gridlines and y labels.
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i;
    root.append(
      svg("line", { x1: M.left, x2: W - M.right, y1: y(v), y2: y(v), class: i === 0 ? "chart-axis" : "chart-grid" }),
      svg("text", { x: M.left - 6, y: y(v) + 4, class: "chart-label chart-label-y", text: Number.isInteger(v) ? String(v) : v.toFixed(1) }));
  }
  // X labels: about six, evenly spaced, anchored at the bar centre.
  const every = Math.max(1, Math.ceil(daily.length / 6));
  daily.forEach((d, i) => {
    if (i % every !== 0 && i !== daily.length - 1) return;
    if (i !== daily.length - 1 && daily.length - 1 - i < every / 2) return;
    root.append(svg("text", { x: M.left + band * i + band / 2, y: H - 6, class: "chart-label chart-label-x", text: shortDay(d.day) }));
  });
  // Bars, one group per day.
  daily.forEach((d, i) => {
    const x = M.left + band * i + (band - barW) / 2;
    let base = 0;
    const parts = series.map((s) => {
      const v = d[s.key] || 0;
      if (!v) return null;
      const top = y(base + v);
      const bar = svg("rect", { x, y: top, width: barW, height: Math.max(0, y(base) - top), class: `bar-seg ${s.cls}` });
      base += v;
      return bar;
    });
    const tip = `${d.day}: ${series.map((s) => `${d[s.key] || 0} ${s.label.toLowerCase()}`).join(", ")}`;
    const hit = svg("rect", { x: M.left + band * i, y: M.top, width: band, height: innerH, class: "chart-hit", tabindex: "0" },
      svg("title", { text: tip }));
    const show = () => { readout.textContent = tip; };
    hit.addEventListener("mouseover", show);
    hit.addEventListener("focus", show);
    hit.addEventListener("mouseout", () => { readout.textContent = summary; });
    hit.addEventListener("blur", () => { readout.textContent = summary; });
    root.append(svg("g", {}, ...parts, hit));
  });

  const legend = h("div", { class: "legend" }, series.map((s) =>
    h("span", { class: "legend-item" }, h("span", { class: `swatch ${s.cls}`, "aria-hidden": "true" }), s.label)));
  return h("figure", { class: "chart-figure" }, h("figcaption", { class: "chart-title", text: title }), root, legend, readout);
}
