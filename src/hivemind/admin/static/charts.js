// Hivemind front-end kit: the daily stacked bar chart (ADR 0029), shared with the portal.
// Drawn at the width it is shown at, so labels keep their size. Every label is set through
// textContent (h(), svg()).

import { h, svg } from "./ui.js";

const HEIGHT = 200;
const M = { left: 36, right: 4, top: 10, bottom: 24 };
const FALLBACK_WIDTH = 640;
const X_LABEL_PX = 64;
const MAX_BAR_PX = 28;

/** A count axis up to `max`: whole-number ticks 1, 2 or 5 times a power of ten apart, at most `intervals` of them. */
export function niceScale(max, intervals = 4) {
  const raw = Math.max(1, max) / intervals;
  const pow = 10 ** Math.floor(Math.log10(raw));
  const step = Math.max(1, [1, 2, 5, 10].map((m) => m * pow).find((s) => s >= raw));
  const top = Math.max(step, Math.ceil(Math.max(1, max) / step) * step);
  const ticks = [];
  for (let t = 0; t <= top; t += step) ticks.push(t);
  return { top, ticks };
}

/** "Nov 14" for the ISO day "2026-11-14", read as a UTC date. */
export function dayLabel(day) {
  return new Date(`${day}T00:00:00Z`).toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

/** The indexes of the days that get an x label: evenly spaced, always the last, never crowding it. */
export function xLabelDays(count, width) {
  if (count <= 0) return [];
  const every = Math.max(1, Math.ceil(count / Math.max(1, Math.floor(width / X_LABEL_PX))));
  const picked = [];
  for (let i = 0; i < count; i += every) if (count - 1 - i >= every || i === count - 1) picked.push(i);
  if (picked[picked.length - 1] !== count - 1) picked.push(count - 1);
  return picked;
}

/** A holder that draws `draw(width)` at its own width and draws again when that width changes. */
function responsive(draw) {
  const holder = h("div", { class: "chart-holder" });
  let width = 0;
  const paint = (next) => {
    if (next <= 0 || Math.abs(next - width) < 1) return;
    width = next;
    holder.replaceChildren(...draw(width));
  };
  paint(FALLBACK_WIDTH);
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(([entry]) => paint(Math.round(entry.contentRect.width))).observe(holder);
  }
  return holder;
}

/**
 * A stacked daily bar chart.
 * `daily` is [{day, ...counts}], `series` is [{key, label, cls}] (bottom first).
 * Returns a figure with the chart, a legend with each series' total, and a tooltip that follows the
 * pointer or the arrow keys.
 */
export function stackedBars({ daily, series, title, showTitle = true }) {
  const value = (d, s) => d[s.key] || 0;
  const totals = daily.map((d) => series.reduce((sum, s) => sum + value(d, s), 0));
  const grand = series.map((s) => daily.reduce((sum, d) => sum + value(d, s), 0));
  const scale = niceScale(Math.max(0, ...totals));
  const summary = `${title}, ${daily.length} days: ${series.map((s, i) => `${s.label} ${grand[i]}`).join(", ")}.`;
  const readout = h("p", { class: "visually-hidden", "aria-live": "polite", text: summary });
  let at = null;

  const tooltipFor = (i) => h("div", { class: "chart-tip", role: "presentation" },
    h("div", { class: "chart-tip-day", text: dayLabel(daily[i].day) }),
    [...series].reverse().filter((s) => series.length <= 3 || value(daily[i], s) > 0).map((s) => h("div", { class: "chart-tip-row" },
      h("span", { class: `swatch ${s.cls}`, "aria-hidden": "true" }), h("span", { text: s.label }), h("span", { class: "num", text: value(daily[i], s) }))),
    series.length > 1 ? h("div", { class: "chart-tip-row chart-tip-total" }, h("span"), h("span", { text: "Total" }), h("span", { class: "num", text: totals[i] })) : null);

  const draw = (width) => {
    const innerW = Math.max(1, width - M.left - M.right);
    const innerH = HEIGHT - M.top - M.bottom;
    const band = daily.length ? innerW / daily.length : innerW;
    const barW = Math.min(MAX_BAR_PX, Math.max(1, band * 0.72));
    const gapped = barW >= 6;
    const y = (v) => M.top + innerH - (v / scale.top) * innerH;
    const left = (i) => M.left + band * i;

    const root = svg("svg", { width, height: HEIGHT, viewBox: `0 0 ${width} ${HEIGHT}`, class: "chart", role: "img", "aria-label": summary });
    for (const t of scale.ticks) {
      root.append(
        svg("line", { x1: M.left, x2: width - M.right, y1: y(t), y2: y(t), class: t === 0 ? "chart-axis" : "chart-grid" }),
        svg("text", { x: M.left - 8, y: y(t) + 4, class: "chart-label chart-label-y", text: t.toLocaleString() }));
    }
    for (const i of xLabelDays(daily.length, innerW)) {
      root.append(svg("text", { x: left(i) + band / 2, y: HEIGHT - 6, class: "chart-label chart-label-x", text: dayLabel(daily[i].day) }));
    }
    const highlight = svg("rect", { y: M.top, width: band, height: innerH, rx: 4, class: "chart-band", visibility: "hidden" });
    root.append(highlight);
    daily.forEach((d, i) => {
      const x = left(i) + (band - barW) / 2;
      let base = 0;
      for (const s of series) {
        const v = value(d, s);
        if (!v) continue;
        const top = y(base + v);
        root.append(svg("rect", { x, y: top, width: barW, height: Math.max(0, y(base) - top), rx: gapped ? 2 : 0, class: `bar-seg ${s.cls}${gapped ? " bar-gap" : ""}` }));
        base += v;
      }
    });

    const tipHolder = h("div", { class: "chart-tip-holder" });
    const hit = svg("rect", { x: M.left, y: M.top, width: innerW, height: innerH, class: "chart-hit", tabindex: "0", "aria-label": `${title}: use the arrow keys to read each day` });
    root.append(hit);

    const show = (i) => {
      if (!daily.length) return;
      at = Math.max(0, Math.min(daily.length - 1, i));
      highlight.setAttribute("x", left(at));
      highlight.setAttribute("visibility", "visible");
      const tip = tooltipFor(at);
      const centre = left(at) + band / 2;
      tip.style.left = `${centre}px`;
      tip.classList.toggle("chart-tip-left", centre > width * 0.6);
      tipHolder.replaceChildren(tip);
      readout.textContent = `${dayLabel(daily[at].day)}: ${series.map((s) => `${s.label} ${value(daily[at], s)}`).join(", ")}`;
    };
    const clear = () => {
      at = null;
      highlight.setAttribute("visibility", "hidden");
      tipHolder.replaceChildren();
      readout.textContent = summary;
    };
    const nearest = (event) => Math.floor((event.clientX - root.getBoundingClientRect().left - M.left) / band);
    hit.addEventListener("mousemove", (event) => show(nearest(event)));
    hit.addEventListener("mouseleave", clear);
    hit.addEventListener("focus", () => show(at ?? daily.length - 1));
    hit.addEventListener("blur", clear);
    hit.addEventListener("keydown", (event) => {
      const moves = { ArrowLeft: (at ?? daily.length) - 1, ArrowRight: (at ?? -1) + 1, Home: 0, End: daily.length - 1 };
      if (!(event.key in moves)) return;
      event.preventDefault();
      show(moves[event.key]);
    });
    if (at !== null) queueMicrotask(() => show(at));
    return [root, tipHolder];
  };

  const legend = h("div", { class: "legend" }, series.map((s, i) => h("span", { class: "legend-item" },
    h("span", { class: `swatch ${s.cls}`, "aria-hidden": "true" }), s.label, h("span", { class: "legend-total", text: grand[i].toLocaleString() }))));
  return h("figure", { class: "chart-figure" },
    showTitle ? h("figcaption", { class: "chart-title", text: title }) : null,
    responsive(draw), legend, readout);
}
