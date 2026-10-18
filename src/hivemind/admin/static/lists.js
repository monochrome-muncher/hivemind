// Hivemind front-end kit: memory and feedback lists.
//
// These know no route, no key and no app state: each takes a page fetcher,
// an "open this memory" callback and a fleet-name lookup, so the admin panel
// and an agent-key app can show the same rows over their own endpoints.
// Summaries, tags and notes are written by agents: plain text only.

import { h, link, toast, fmtAgo, fmtTime, kindBadge, stateBadge, verdictBadge } from "./ui.js";

export const PAGE = 50;
const sameName = (id) => id;
const noHref = () => null;
// A name as a link to its page when the app has one, else plain text.
const nameCell = (name, agentHref, strong = false) => {
  const href = agentHref(name);
  if (href) return link(name, href, { class: strong ? "link strong" : "link" });
  return strong ? h("strong", { text: name }) : name;
};
const scopeText = (scope, fleetId, fleetName) => (fleetId ? `${scope} · ${fleetName(fleetId)}` : scope);

/**
 * A table that loads `PAGE` rows at a time. `fetchPage({limit, offset})`
 * resolves to an array of rows (or undefined to stop, e.g. after a sign-out);
 * `renderRow(row)` builds a <tr>. Returns {el, tbody, load(reset)}: load(true)
 * starts again from the top, load(false) appends the next page.
 */
export function pagedTable({ headers, className = "entries", fetchPage, renderRow, emptyText, statusText }) {
  const tbody = h("tbody");
  const status = h("p", { class: "muted small result-status", "aria-live": "polite" });
  const more = h("button", { type: "button", class: "btn", text: "Load more" });
  const moreWrap = h("div", { class: "load-more", hidden: true }, more);
  let offset = 0;
  let ticket = 0;
  let shown = 0;
  let resetting = false; // a "Load more" during a reset becomes part of it

  const load = async (reset) => {
    if (resetting) reset = true;
    const mine = ++ticket;
    resetting = reset;
    more.disabled = true;
    try {
      const rows = await fetchPage({ limit: PAGE, offset: reset ? 0 : offset });
      if (!rows || mine !== ticket) return;
      // Counters move only on a response that is still current, so a failed
      // or superseded reset never leaves old rows with a reset offset.
      if (reset) { tbody.replaceChildren(); offset = 0; shown = 0; }
      offset += rows.length;
      shown += rows.length;
      tbody.append(...rows.map(renderRow));
      if (shown === 0) tbody.replaceChildren(h("tr", {}, h("td", { class: "empty", colspan: headers.length, text: emptyText })));
      status.textContent = statusText(shown);
      moreWrap.hidden = rows.length < PAGE;
    } catch (err) {
      if (mine !== ticket) return;
      toast(err.message, true);
      if (reset) moreWrap.hidden = true; // the rows shown no longer match the filters
    } finally {
      if (mine === ticket) { more.disabled = false; resetting = false; }
    }
  };
  more.addEventListener("click", () => load(false));

  const el = h("div", { class: "card" },
    h("div", { class: "card-body card-body-tight" }, status),
    h("div", { class: "table-wrap" }, h("table", { class: className },
      h("thead", {}, h("tr", {}, headers.map((t) => h("th", { text: t })))), tbody)),
    moreWrap);
  return { el, tbody, load };
}

// --- memories ------------------------------------------------------------------------

export const ENTRY_HEADERS = ["Memory", "Kind", "Scope", "Author", "Written", "State", "Flags"];

/** Normalise a listing row (EntryOut) or a search hit (HitOut) to one shape. */
export function normaliseEntry(x) {
  const fb = x.feedback || null;
  return {
    id: x.id || x.entry_id,
    kind: x.kind,
    summary: x.summary,
    tags: x.tags || [],
    scope: x.scope,
    fleet_id: x.fleet_id,
    author: x.author,
    agent: x.agent,
    when: x.created_at || x.occurred_at,
    state: x.state || "active",
    feedback: fb && (fb.helpful || fb.stale || fb.wrong) ? fb : null,
    score: x.score,
  };
}

/** One memory as a clickable row; `compact` drops the Flags column. */
export function entryRow(item, { onOpen, compact = false, fleetName = sameName } = {}) {
  const open = () => onOpen(item.id);
  const row = h("tr", { class: "clickable", tabindex: "0", role: "button", "aria-label": `Open memory: ${item.summary}` });
  row.addEventListener("click", open);
  row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
  row.append(...[
    h("td", { class: "cell-summary" }, h("div", { class: "summary-text", text: item.summary }),
      item.tags.length ? h("div", { class: "chips chips-sm" }, item.tags.slice(0, 5).map((t) => h("span", { class: "chip", text: t }))) : null),
    h("td", {}, kindBadge(item.kind)),
    h("td", { class: "nowrap", text: scopeText(item.scope, item.fleet_id, fleetName) }),
    h("td", {}, item.author, item.agent && item.agent !== item.author ? h("div", { class: "muted small", text: `via ${item.agent}` }) : null),
    h("td", { class: "nowrap", title: fmtTime(item.when), text: fmtAgo(item.when) }),
    h("td", {}, stateBadge(item.state)),
    compact ? null : h("td", { class: "nowrap" }, item.feedback
      ? [item.feedback.helpful ? verdictBadge("helpful", item.feedback.helpful) : null, " ",
        item.feedback.stale ? verdictBadge("stale", item.feedback.stale) : null, " ",
        item.feedback.wrong ? verdictBadge("wrong", item.feedback.wrong) : null]
      : h("span", { class: "muted", text: "—" })),
  ].filter(Boolean));
  return row;
}

/** A paged memory list over `fetchPage` (rows as EntryOut or HitOut). */
export function entryTable({ fetchPage, onOpen, fleetName = sameName, emptyText = "No memories match.", statusText }) {
  return pagedTable({
    headers: ENTRY_HEADERS,
    fetchPage: async (page) => {
      const rows = await fetchPage(page);
      return rows && rows.map(normaliseEntry);
    },
    renderRow: (item) => entryRow(item, { onOpen, fleetName }),
    emptyText,
    statusText: statusText || ((n) => `${n} shown, newest first`),
  });
}

// --- feedback ------------------------------------------------------------------------

export const FEEDBACK_HEADERS = ["Given", "Reporter", "Verdict", "Note", "Memory", "Author"];

/** One feedback log row: who gave which verdict on which memory. */
export function feedbackRow(row, { onOpen, fleetName = sameName, agentHref = noHref } = {}) {
  const e = row.entry;
  return h("tr", {},
    h("td", { class: "nowrap", title: fmtTime(row.updated_at), text: fmtAgo(row.updated_at) }),
    h("td", { class: "nowrap" }, nameCell(row.reporter, agentHref, true),
      row.agent && row.agent !== row.reporter ? h("div", { class: "muted small", text: `as ${row.agent}` }) : null),
    h("td", {}, verdictBadge(row.verdict)),
    h("td", { class: "cell-note" }, row.note ? h("div", { class: "plain", text: row.note }) : h("span", { class: "muted", text: "—" })),
    h("td", { class: "cell-summary" },
      h("button", { type: "button", class: "linklike summary-text", text: e.summary, onclick: () => onOpen(e.id) }),
      h("div", { class: "row tight" }, kindBadge(e.kind),
        h("span", { class: "muted small", text: scopeText(e.scope, e.fleet_id, fleetName) }),
        e.state !== "active" ? stateBadge(e.state) : null)),
    h("td", { class: "nowrap" }, nameCell(e.author, agentHref)));
}

/** A paged feedback log over `fetchPage` (rows in the feedback log shape, ADR 0090). */
export function feedbackTable({ fetchPage, onOpen, fleetName = sameName, agentHref = noHref, emptyText = "No feedback matches." }) {
  return pagedTable({
    headers: FEEDBACK_HEADERS,
    className: "entries feedback-log",
    fetchPage,
    renderRow: (row) => feedbackRow(row, { onOpen, fleetName, agentHref }),
    emptyText,
    statusText: (n) => `${n} shown, newest first`,
  });
}
