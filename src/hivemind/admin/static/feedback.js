// Hivemind admin panel: the Feedback log (who rated what).
// Every verdict, newest first, each with the memory it is about, from
// GET admin/feedback. Filters live in the hash query so other views can
// deep-link: #/feedback?reporter=bob&verdict=wrong&author=alice&fleet=<id>&entry=<id>.
//
// feedbackTable() knows nothing about the admin routes: it takes a page
// fetcher and an "open this memory" callback, so another front end can
// reuse it over its own endpoint. Notes are written by agents: plain text.

import {
  api, h, state, toast, guarded, parseHash, loadAgentsAndFleets, fleetName, fmtAgo, fmtTime, kindBadge, stateBadge,
  verdictBadge, pageHead, refreshButton, card, link, memoriesHref, VERDICTS,
} from "./core.js";
import { openEntry } from "./drawer.js";

const PAGE = 50;
const HEADERS = ["Given", "Reporter", "Verdict", "Note", "Memory", "Author"];

function feedbackRow(row, onOpen) {
  const e = row.entry;
  return h("tr", {},
    h("td", { class: "nowrap", title: fmtTime(row.updated_at), text: fmtAgo(row.updated_at) }),
    h("td", { class: "nowrap" }, h("strong", { text: row.reporter }),
      row.agent && row.agent !== row.reporter ? h("div", { class: "muted small", text: `as ${row.agent}` }) : null),
    h("td", {}, verdictBadge(row.verdict)),
    h("td", { class: "cell-note" }, row.note ? h("div", { class: "plain", text: row.note }) : h("span", { class: "muted", text: "—" })),
    h("td", { class: "cell-summary" },
      h("button", { type: "button", class: "linklike summary-text", text: e.summary, onclick: () => onOpen(e.id) }),
      h("div", { class: "row tight" }, kindBadge(e.kind),
        h("span", { class: "muted small", text: e.fleet_id ? `${e.scope} · ${fleetName(e.fleet_id)}` : e.scope }),
        e.state !== "active" ? stateBadge(e.state) : null)),
    h("td", { class: "nowrap", text: e.author }));
}

/**
 * A paged feedback table. `fetchPage({limit, offset})` resolves to an array
 * of feedback log rows (or undefined to stop); `onOpen(entryId)` opens a
 * memory. Returns {el, load(reset)} where load(true) starts from the top.
 */
export function feedbackTable({ fetchPage, onOpen, emptyText = "No feedback matches." }) {
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
      tbody.append(...rows.map((r) => feedbackRow(r, onOpen)));
      if (shown === 0) tbody.replaceChildren(h("tr", {}, h("td", { class: "empty", colspan: HEADERS.length, text: emptyText })));
      status.textContent = `${shown} shown, newest first`;
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
    h("div", { class: "table-wrap" }, h("table", { class: "entries feedback-log" },
      h("thead", {}, h("tr", {}, HEADERS.map((t) => h("th", { text: t })))), tbody)),
    moreWrap);
  return { el, load };
}

export async function viewFeedback(root) {
  await loadAgentsAndFleets();
  const { params } = parseHash();
  const get = (k) => params.get(k) || "";

  const names = h("datalist", { id: "fb-names" }, state.agents.map((a) => h("option", { value: a.name })));
  const reporter = h("input", { type: "text", list: "fb-names", value: get("reporter"), placeholder: "Reporter (agent name)", "aria-label": "Reporter" });
  const verdict = h("select", { "aria-label": "Verdict" }, h("option", { value: "", text: "Any verdict" }),
    VERDICTS.map((v) => h("option", { value: v, text: v, selected: v === get("verdict") })));
  const author = h("input", { type: "text", list: "fb-names", value: get("author"), placeholder: "Memory author", "aria-label": "Memory author" });
  const fleet = h("select", { "aria-label": "Fleet" }, h("option", { value: "", text: "All fleets" }),
    state.fleets.map((f) => h("option", { value: f.id, text: f.name, selected: f.id === get("fleet") })));
  // One memory's feedback (from the drawer's "all feedback" link): shown as a
  // removable chip, not an input, since nobody types an entry id.
  let entry = get("entry");
  const entryChip = h("span", { class: "chip chip-filter", hidden: !entry });
  const paintEntry = () => {
    entryChip.hidden = !entry;
    entryChip.replaceChildren(entry ? `One memory: ${entry.slice(0, 8)}…` : "", " ",
      h("button", { type: "button", class: "linklike", "aria-label": "Show every memory", text: "×", onclick: () => { entry = ""; paintEntry(); table.load(true); } }));
  };
  paintEntry();
  const apply = h("button", { type: "submit", class: "btn btn-primary", text: "Apply" });
  const reset = h("button", { type: "button", class: "btn btn-ghost", text: "Clear" });
  const form = h("form", { class: "filters filters-wide" }, reporter, verdict, author, names, fleet, entryChip, apply, reset);

  const filters = () => ({
    reporter: reporter.value.trim(), verdict: verdict.value, author: author.value.trim(), fleet: fleet.value, entry,
  });
  const syncHash = () => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(filters())) if (v) p.set(k, v);
    const next = `#/feedback${p.toString() ? "?" + p.toString() : ""}`;
    if (root.dataset.superseded) return; // the user navigated away while this view loaded
    if (window.location.hash !== next) history.replaceState(null, "", next);
  };

  const table = feedbackTable({
    fetchPage: ({ limit, offset }) => {
      syncHash();
      const f = filters();
      return guarded(() => api("GET", "admin/feedback", {
        query: { reporter: f.reporter, verdict: f.verdict, author: f.author, fleet_id: f.fleet, entry_id: f.entry, limit, offset },
      }));
    },
    onOpen: (id) => openEntry(id, () => table.load(true)),
  });

  form.addEventListener("submit", (event) => { event.preventDefault(); table.load(true); });
  for (const el of [verdict, fleet, reporter, author]) el.addEventListener("change", () => table.load(true));
  reset.addEventListener("click", () => {
    reporter.value = ""; verdict.value = ""; author.value = ""; fleet.value = ""; entry = ""; paintEntry();
    table.load(true);
  });

  root.replaceChildren(
    pageHead("Feedback", "Who rated which memory, newest first. A reporter's latest verdict on a memory replaces its earlier one.",
      link("Memories", memoriesHref(), { class: "link small" }), refreshButton()),
    card(null, h("div", {}, form), { className: "card-filters" }),
    table.el,
    h("p", { class: "muted small note", text: "Verdicts from revoked agents are left out, as in every count. Notes are written by agents: shown as plain text, not checked by anyone." }));
  await table.load(true);
}
