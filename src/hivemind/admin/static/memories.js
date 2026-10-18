// Hivemind admin panel: the Memories view (ADR 0029).
// Search (POST admin/search) when there is text, otherwise the newest-first
// listing (GET admin/entries). Filters live in the hash query so other views
// can deep-link: #/memories?author=alice&fleet=<id>.

import {
  api, apiRaw, h, state, toast, guarded, parseHash, loadAgentsAndFleets, fleetName, pageHead, refreshButton, card, KINDS, SCOPES, fill,
} from "./core.js";
import { openEntry } from "./drawer.js";
import { ENTRY_HEADERS, PAGE, entryCsvColumns, entryRow, exportButton, normaliseEntry } from "./lists.js";

export async function viewMemories(root) {
  await loadAgentsAndFleets();
  const { params } = parseHash();
  const get = (k) => params.get(k) || "";

  const q = h("input", { type: "search", id: "m-q", value: get("q"), placeholder: "Search memories…", "aria-label": "Search memories" });
  const fleet = h("select", { "aria-label": "Fleet" }, h("option", { value: "", text: "All fleets" }),
    state.fleets.map((f) => h("option", { value: f.id, text: f.name, selected: f.id === get("fleet") })));
  const authors = h("datalist", { id: "m-authors" }, state.agents.map((a) => h("option", { value: a.name })));
  const author = h("input", { type: "text", list: "m-authors", value: get("author"), placeholder: "Author (agent name)", "aria-label": "Author" });
  const kind = h("select", { "aria-label": "Kind" }, h("option", { value: "", text: "Any kind" }),
    KINDS.map((k) => h("option", { value: k, text: k, selected: k === get("kind") })));
  const scope = h("select", { "aria-label": "Scope" }, h("option", { value: "", text: "Any scope" }),
    SCOPES.map((s) => h("option", { value: s, text: s, selected: s === get("scope") })));
  const stateSel = h("select", { "aria-label": "State" },
    h("option", { value: "", text: "Active only", selected: get("all") !== "1" }),
    h("option", { value: "1", text: "Include inactive", selected: get("all") === "1" }));
  const flagged = h("input", { type: "checkbox", id: "m-flagged" });
  flagged.checked = get("flagged") === "1";
  const apply = h("button", { type: "submit", class: "btn btn-primary", text: "Search" });
  const reset = h("button", { type: "button", class: "btn btn-ghost", text: "Clear" });
  const form = h("form", { class: "filters filters-wide" },
    q, fleet, author, authors, kind, scope, stateSel,
    h("label", { class: "check", for: "m-flagged" }, flagged, " Flagged only"), apply, reset);

  const tbody = h("tbody");
  const status = h("p", { class: "muted small result-status", "aria-live": "polite" });
  const degraded = h("p", { class: "callout callout-warn small", hidden: true, text: "The embedder is unavailable: results are keyword-only." });
  const more = h("button", { type: "button", class: "btn", text: "Load more" });
  const moreWrap = h("div", { class: "load-more", hidden: true }, more);

  let offset = 0; // both modes page by offset (limit stays PAGE)
  let ticket = 0;
  let generation = 0; // bumped when the filters are applied afresh, so an export can tell
  let applied = null; // the filters the table shows (the search box may hold unsent text)

  const filters = () => ({
    q: q.value.trim(), fleet: fleet.value, author: author.value.trim(), kind: kind.value, scope: scope.value,
    all: stateSel.value, flagged: flagged.checked ? "1" : "",
  });
  const syncHash = () => {
    const f = filters();
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(f)) if (v) p.set(k, v);
    const next = `#/memories${p.toString() ? "?" + p.toString() : ""}`;
    if (root.dataset.superseded) return; // the user navigated away while this view loaded
    if (window.location.hash !== next) history.replaceState(null, "", next);
  };

  // One page under filters `f`: search when there is text, else the listing.
  // Resolves to {items, degraded}, or undefined after a sign-out.
  const fetchItems = async (f, limit, offset) => {
    if (f.q) {
      const body = { query: f.q, limit, offset };
      if (f.kind) body.kind = f.kind;
      if (f.scope) body.scope = f.scope;
      if (f.fleet) body.fleet_id = f.fleet;
      if (f.author) body.author = f.author;
      if (f.all) body.include_inactive = true;
      if (f.flagged) body.flagged = true;
      const res = await guarded(() => apiRaw("POST", "admin/search", { body }));
      if (!res) return undefined;
      return { items: (res.payload || []).map(normaliseEntry), degraded: (res.headers.get("X-Hivemind-Degraded") || "") === "keyword-only" };
    }
    const query = {
      kind: f.kind, scope: f.scope, fleet_id: f.fleet, author: f.author, include_inactive: f.all ? "true" : "",
      flagged: f.flagged ? "true" : "", limit, offset,
    };
    const res = await guarded(() => api("GET", "admin/entries", { query }));
    return res && { items: res.map(normaliseEntry), degraded: false };
  };

  const load = async (append) => {
    const f = filters();
    const mine = ++ticket;
    if (!append) { generation += 1; applied = f; }
    syncHash();
    more.disabled = true;
    try {
      if (!append) offset = 0;
      const page = await fetchItems(f, PAGE, offset);
      if (!page || mine !== ticket) return;
      degraded.hidden = !page.degraded;
      const items = page.items;
      if (!append) tbody.replaceChildren();
      offset += items.length;
      const open = (id) => openEntry(id, () => load(false));
      tbody.append(...items.map((it) => entryRow(it, { onOpen: open, fleetName })));
      const count = tbody.querySelectorAll("tr.clickable").length;
      if (count === 0) {
        tbody.replaceChildren(h("tr", {}, h("td", { class: "empty", colspan: 7, text: f.q ? "No memories match this search." : "No memories match these filters." })));
      }
      status.textContent = f.q ? `${count} result${count === 1 ? "" : "s"}, best match first` : `${count} shown, newest first`;
      moreWrap.hidden = items.length < PAGE;
    } catch (err) {
      if (mine === ticket) toast(err.message, true);
    } finally {
      more.disabled = false;
    }
  };
  const exporter = exportButton({
    fetchPage: async ({ limit, offset: at }) => {
      const page = await fetchItems(applied || filters(), limit, at);
      return page && page.items;
    },
    columns: entryCsvColumns(fleetName),
    name: "memories",
    generation: () => generation,
  });

  form.addEventListener("submit", (event) => { event.preventDefault(); load(false); });
  for (const el of [fleet, kind, scope, stateSel, flagged]) el.addEventListener("change", () => load(false));
  author.addEventListener("change", () => load(false));
  reset.addEventListener("click", () => {
    q.value = ""; fleet.value = ""; author.value = ""; kind.value = ""; scope.value = ""; stateSel.value = ""; flagged.checked = false;
    load(false);
  });
  more.addEventListener("click", () => load(true));

  fill(root, 
    pageHead("Memories", "What the agents have written. Search ranks like an agent's search; the list is newest first.", refreshButton()),
    card(null, h("div", {}, form), { className: "card-filters" }),
    degraded,
    h("div", { class: "card" },
      h("div", { class: "card-body card-body-tight result-bar" }, status, exporter),
      h("div", { class: "table-wrap" }, h("table", { class: "entries" },
        h("thead", {}, h("tr", {}, ENTRY_HEADERS.map((t) => h("th", { text: t })))),
        tbody)),
      moreWrap));
  await load(false);
}
