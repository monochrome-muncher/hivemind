// Hivemind front-end kit: memory and feedback lists.
//
// These know no route, no key and no app state: each takes a page fetcher,
// an "open this memory" callback and a fleet-name lookup, so the admin panel
// and the agent portal show the same rows over their own endpoints.
// Summaries, tags and notes are written by agents: plain text only.

import { h, link, toast, fmtAgo, fmtTime, kindBadge, stateBadge, verdictBadge } from "./ui.js";
import { EXPORT_MAX, column, csvName, downloadCsv, fetchAll, toCsv } from "./csv.js";

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
 * starts again from the top, load(false) appends the next page; `fetchPage`
 * gets `fresh: true` on a reload from the top, the moment a caller should
 * read its filters (later pages and an export reuse them). With `csv`
 * ({columns, name, key}) it also offers every row under those filters as a
 * CSV file.
 */
export function pagedTable({ headers, className = "entries", fetchPage, renderRow, emptyText, statusText, csv = null }) {
  const tbody = h("tbody");
  const status = h("p", { class: "muted small result-status", "aria-live": "polite" });
  let generation = 0; // bumped on every reset, so an export can tell the filters changed
  const more = h("button", { type: "button", class: "btn", text: "Load more" });
  const moreWrap = h("div", { class: "load-more", hidden: true }, more);
  let offset = 0;
  let ticket = 0;
  let shown = 0;
  let resetting = false; // a "Load more" during a reset becomes part of it

  const load = async (reset) => {
    if (resetting) reset = true;
    if (reset) generation += 1;
    const mine = ++ticket;
    resetting = reset;
    more.disabled = true;
    try {
      const rows = await fetchPage({ limit: PAGE, offset: reset ? 0 : offset, fresh: reset });
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
    h("div", { class: "card-body card-body-tight result-bar" }, status,
      csv ? exportButton({ fetchPage, ...csv, generation: () => generation }) : null),
    h("div", { class: "table-wrap" }, h("table", { class: className },
      h("thead", {}, h("tr", {}, headers.map((t) => h("th", { text: t })))), tbody)),
    moreWrap);
  return { el, tbody, load };
}

/**
 * An "Export CSV" button: fetches every row `fetchPage` gives (up to
 * EXPORT_MAX) and saves them under `columns` as hivemind-<name>-<date>.csv.
 * `generation()` changes whenever the list reloads from the top; an export
 * that sees it change stops rather than risk mixing two filters in one file.
 * `fetchPage` must read the filters the list shows, not unsent form input.
 * `key(row)` drops a row a shifting page returns twice.
 */
export function exportButton({ fetchPage, columns, name, key = null, generation = () => 0 }) {
  const button = h("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Export CSV" });
  button.addEventListener("click", async () => {
    const started = generation();
    button.disabled = true;
    button.textContent = "Exporting…";
    try {
      const result = await fetchAll(fetchPage, { key, cancelled: () => generation() !== started });
      if (!result) {
        if (generation() !== started) toast("The list was reloaded during the export, so it stopped. Export again.", true);
        return;
      }
      downloadCsv(csvName(name), toCsv(columns, result.rows));
      toast(result.truncated
        ? `Exported the first ${EXPORT_MAX.toLocaleString()} rows; narrow the filters for the rest.`
        : `Exported ${result.rows.length} row${result.rows.length === 1 ? "" : "s"}.`);
    } catch (err) {
      toast(err.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "Export CSV";
    }
  });
  return button;
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
    created_at: x.created_at,
    occurred_at: x.occurred_at,
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

const fleetColumns = (fleetName, id = (r) => r.fleet_id, prefix = "") => [
  column(`${prefix}fleet_id`, id),
  column(`${prefix}fleet`, (r) => (id(r) ? fleetName(id(r)) : "")),
];
const countColumns = (counts) => ["helpful", "stale", "wrong"].map((v) => column(v, (r) => (counts(r) || {})[v] || 0));

/** CSV columns for a memory list (rows from normaliseEntry). */
export const entryCsvColumns = (fleetName = sameName) => [
  column("id"), column("kind"), column("summary"), column("tags"), column("scope"), ...fleetColumns(fleetName),
  column("author"), column("agent"), column("created_at"), column("occurred_at"), column("state"),
  ...countColumns((r) => r.feedback), column("score"),
];

/** A paged memory list over `fetchPage` (rows as EntryOut or HitOut); `csvName` adds an export. */
export function entryTable({ fetchPage, onOpen, fleetName = sameName, emptyText = "No memories match.", statusText, csvName: name = null }) {
  return pagedTable({
    headers: ENTRY_HEADERS,
    fetchPage: async (page) => {
      const rows = await fetchPage(page);
      return rows && rows.map(normaliseEntry);
    },
    renderRow: (item) => entryRow(item, { onOpen, fleetName }),
    emptyText,
    statusText: statusText || ((n) => `${n} shown, newest first`),
    csv: name ? { columns: entryCsvColumns(fleetName), name, key: (r) => r.id } : null,
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

/** CSV columns for a feedback log (rows in the ADR 0090 shape). */
export const feedbackCsvColumns = (fleetName = sameName) => [
  column("updated_at"), column("reporter"), column("agent"), column("verdict"), column("note"),
  column("entry_id", (r) => r.entry.id), column("entry_summary", (r) => r.entry.summary), column("entry_kind", (r) => r.entry.kind),
  column("entry_scope", (r) => r.entry.scope), ...fleetColumns(fleetName, (r) => r.entry.fleet_id, "entry_"),
  column("entry_author", (r) => r.entry.author), column("entry_state", (r) => r.entry.state),
];

/** A paged feedback log over `fetchPage` (rows in the feedback log shape, ADR 0090); `csvName` adds an export. */
export function feedbackTable({ fetchPage, onOpen, fleetName = sameName, agentHref = noHref, emptyText = "No feedback matches.", csvName: name = null }) {
  return pagedTable({
    headers: FEEDBACK_HEADERS,
    className: "entries feedback-log",
    fetchPage,
    renderRow: (row) => feedbackRow(row, { onOpen, fleetName, agentHref }),
    emptyText,
    statusText: (n) => `${n} shown, newest first`,
    csv: name ? { columns: feedbackCsvColumns(fleetName), name, key: (r) => JSON.stringify([r.entry.id, r.reporter, r.agent]) } : null,
  });
}

// --- pins ------------------------------------------------------------------------------

export const PIN_HEADERS = ["Memory", "Kind", "Author", "Pinned by", "Pinned", "Since pinned"];
const CHANGE_TEXT = { superseded: "superseded", flagged: "flagged" };

/** One pinned memory (GET /v1/pins shape): the current version, who pinned it, and what changed since.
 * `extra(pin)` may add a last cell (the portal's Unpin button). */
export function pinRow(pin, { onOpen, agentHref = noHref, extra = null } = {}) {
  return h("tr", {},
    h("td", { class: "cell-summary" },
      h("button", { type: "button", class: "linklike summary-text", text: pin.summary, onclick: () => onOpen(pin.id) }),
      pin.state !== "active" ? h("div", {}, stateBadge(pin.state)) : null),
    h("td", {}, kindBadge(pin.kind)),
    h("td", { class: "nowrap" }, nameCell(pin.author, agentHref)),
    h("td", { class: "nowrap" }, nameCell(pin.pinned_by, agentHref)),
    h("td", { class: "nowrap", title: fmtTime(pin.pinned_at), text: fmtAgo(pin.pinned_at) }),
    h("td", {}, pin.changed_since_pin
      ? h("span", { class: "badge badge-pending", title: "A fleet member should re-pin or unpin it", text: CHANGE_TEXT[pin.change_reason] || "changed" })
      : h("span", { class: "muted", text: "unchanged" })),
    extra ? h("td", { class: "nowrap" }, extra(pin)) : null);
}

// --- flagged memories ------------------------------------------------------------------

export const FLAGGED_HEADERS = ["Memory", "Kind", "Scope", "Author", "First flagged", "Last flagged", "Verdicts"];

/** One memory with a standing stale or wrong verdict (ADR 0093 shape), oldest flag first. */
export function flaggedRow(row, { onOpen, fleetName = sameName, agentHref = noHref } = {}) {
  return h("tr", {},
    h("td", { class: "cell-summary" },
      h("button", { type: "button", class: "linklike summary-text", text: row.summary, onclick: () => onOpen(row.id) })),
    h("td", {}, kindBadge(row.kind)),
    h("td", { class: "nowrap", text: scopeText(row.scope, row.fleet_id, fleetName) }),
    h("td", { class: "nowrap" }, nameCell(row.author, agentHref),
      row.orphaned ? h("div", {}, h("span", { class: "badge badge-revoked", title: "The author is revoked or has not written for 30 days: nobody is likely to fix it", text: "no owner" })) : null),
    h("td", { class: "nowrap", title: fmtTime(row.first_flag_at), text: fmtAgo(row.first_flag_at) }),
    h("td", { class: "nowrap", title: fmtTime(row.last_flag_at), text: fmtAgo(row.last_flag_at) }),
    h("td", { class: "nowrap" },
      row.helpful ? verdictBadge("helpful", row.helpful) : null, " ",
      row.stale ? verdictBadge("stale", row.stale) : null, " ",
      row.wrong ? verdictBadge("wrong", row.wrong) : null));
}

/** CSV columns for the flag backlog (rows in the ADR 0093 shape). */
export const flaggedCsvColumns = (fleetName = sameName) => [
  column("id"), column("kind"), column("summary"), column("scope"), ...fleetColumns(fleetName), column("author"),
  column("orphaned"), column("first_flag_at"), column("last_flag_at"), ...countColumns((r) => r),
];

/** A paged list of flagged memories over `fetchPage`, oldest first flag first; `csvName` adds an export. */
export function flaggedTable({ fetchPage, onOpen, fleetName = sameName, agentHref = noHref, emptyText = "No flagged memory matches.", csvName: name = null }) {
  return pagedTable({
    headers: FLAGGED_HEADERS,
    fetchPage,
    renderRow: (row) => flaggedRow(row, { onOpen, fleetName, agentHref }),
    emptyText,
    statusText: (n) => `${n} shown, oldest flag first`,
    csv: name ? { columns: flaggedCsvColumns(fleetName), name, key: (r) => r.id } : null,
  });
}
