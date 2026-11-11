// Hivemind admin panel: the Memories view (ADR 0029).
// Search (POST admin/search) when there is text, the one memory (GET
// admin/entries/{id}) when the text is a memory id, otherwise the
// newest-first listing (GET admin/entries). Filters live in the hash query so other views
// can deep-link: #/memories?author=alice&fleet=<id>. With "as=<agent>" a search runs as
// that agent would (ADR 0136), with what it cannot see listed below. "explain=1" adds, under
// each search hit, the parts of its score (ADR 0137).

import {
  api, apiRaw, h, state, entryById, entryIdIn, toast, guarded, parseHash, loadAgentsAndFleets, fleetName, pageHead, refreshButton, card, KINDS, SCOPES, fill,
} from "./core.js";
import { asAgentBanner, hiddenFrom } from "./asagent.js";
import { explainLines } from "./explain.js";
import { openEntry } from "./drawer.js";
import { ENTRY_HEADERS, PAGE, entryCsvColumns, entryRow, exportButton, normaliseEntry } from "./lists.js";

export async function viewMemories(root) {
  await loadAgentsAndFleets();
  const { params } = parseHash();
  const get = (k) => params.get(k) || "";

  const q = h("input", { type: "search", id: "m-q", value: get("q"), placeholder: "Search memories or paste an id…", "aria-label": "Search memories or paste an id" });
  const fleet = h("select", { "aria-label": "Fleet" }, h("option", { value: "", text: "All fleets" }),
    state.fleets.map((f) => h("option", { value: f.id, text: f.name, selected: f.id === get("fleet") })));
  const authors = h("datalist", { id: "m-authors" }, state.agents.map((a) => h("option", { value: a.name })));
  const author = h("input", { type: "text", list: "m-authors", value: get("author"), placeholder: "Author (agent name)", "aria-label": "Author" });
  const activeAgents = h("datalist", { id: "m-as-agents" },
    state.agents.filter((a) => a.status === "active").map((a) => h("option", { value: a.name })));
  const asAgent = h("input", { type: "text", list: "m-as-agents", value: get("as"), placeholder: "As agent (search only)", "aria-label": "Search as agent" });
  const kind = h("select", { "aria-label": "Kind" }, h("option", { value: "", text: "Any kind" }),
    KINDS.map((k) => h("option", { value: k, text: k, selected: k === get("kind") })));
  const scope = h("select", { "aria-label": "Scope" }, h("option", { value: "", text: "Any scope" }),
    SCOPES.map((s) => h("option", { value: s, text: s, selected: s === get("scope") })));
  const stateSel = h("select", { "aria-label": "State" },
    h("option", { value: "", text: "Active only", selected: get("all") !== "1" }),
    h("option", { value: "1", text: "Include inactive", selected: get("all") === "1" }));
  const flagged = h("input", { type: "checkbox", id: "m-flagged" });
  flagged.checked = get("flagged") === "1";
  const explain = h("input", { type: "checkbox", id: "m-explain" });
  explain.checked = get("explain") === "1";
  const apply = h("button", { type: "submit", class: "btn btn-primary", text: "Search" });
  const reset = h("button", { type: "button", class: "btn btn-ghost", text: "Clear" });
  const form = h("form", { class: "filters filters-wide" },
    q, asAgent, activeAgents, fleet, author, authors, kind, scope, stateSel,
    h("label", { class: "check", for: "m-flagged" }, flagged, " Flagged only"),
    h("label", { class: "check", for: "m-explain" }, explain, " Explain ranking"), apply, reset);

  const tbody = h("tbody");
  const status = h("p", { class: "muted small result-status", "aria-live": "polite" });
  const degraded = h("p", { class: "callout callout-warn small", hidden: true, text: "The embedder is unavailable: results are keyword-only." });
  const asNote = h("p", { class: "callout callout-info small", hidden: true, text: "\"As agent\" applies to a text search only: the plain list and a memory id show everything." });
  const asBanner = h("p", { class: "callout callout-info small", hidden: true });
  const hiddenBody = h("tbody");
  const hiddenCard = h("div", { class: "card", hidden: true },
    h("div", { class: "card-body card-body-tight" }, h("strong", { class: "hidden-title" }), h("p", { class: "muted small", text: "Found by the admin search, not by this agent's own search." })),
    h("div", { class: "table-wrap" }, h("table", { class: "entries" },
      h("thead", {}, h("tr", {}, [...ENTRY_HEADERS, "Why hidden"].map((t) => h("th", { text: t })))),
      hiddenBody)));
  const more = h("button", { type: "button", class: "btn", text: "Load more" });
  const moreWrap = h("div", { class: "load-more", hidden: true }, more);

  let offset = 0; // both modes page by offset (limit stays PAGE)
  let ticket = 0;
  let generation = 0; // bumped when the filters are applied afresh, so an export can tell
  let applied = null; // the filters the table shows (the search box may hold unsent text)

  const filters = () => ({
    q: q.value.trim(), fleet: fleet.value, author: author.value.trim(), kind: kind.value, scope: scope.value,
    all: stateSel.value, flagged: flagged.checked ? "1" : "", as: asAgent.value.trim(),
    explain: explain.checked ? "1" : "",
  });
  const syncHash = (f) => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(f)) if (v) p.set(k, v);
    const next = `#/memories${p.toString() ? "?" + p.toString() : ""}`;
    if (root.dataset.superseded) return; // the user navigated away while this view loaded
    if (window.location.hash !== next) history.replaceState(null, "", next);
  };

  const searchBody = (f, limit, offset) => {
    const body = { query: f.q, limit, offset };
    if (f.kind) body.kind = f.kind;
    if (f.scope) body.scope = f.scope;
    if (f.fleet) body.fleet_id = f.fleet;
    if (f.author) body.author = f.author;
    if (f.all) body.include_inactive = true;
    if (f.flagged) body.flagged = true;
    if (f.explain) body.explain = true;
    return body;
  };
  // One search page, as agent `as` when given. Resolves to {items, degraded}, or undefined after a sign-out.
  const searchPage = async (body, as = "") => {
    const res = await guarded(() => apiRaw("POST", "admin/search", { body: as ? { ...body, as_agent: as } : body }));
    if (!res) return undefined;
    return { items: (res.payload || []).map(normaliseEntry), degraded: (res.headers.get("X-Hivemind-Degraded") || "") === "keyword-only" };
  };
  // One page under filters `f`: the one memory when the text is an id,
  // search when there is other text, else the listing. An id ignores the
  // other filters. Resolves to {items, degraded}, or undefined after a sign-out.
  const fetchItems = async (f, limit, offset) => {
    const id = entryIdIn(f.q);
    if (id) {
      if (offset > 0) return { items: [], degraded: false };
      const items = await entryById(() => guarded(() => api("GET", `admin/entries/${encodeURIComponent(id)}`)));
      return items && { items: items.map(normaliseEntry), degraded: false };
    }
    if (f.q) return searchPage(searchBody(f, limit, offset), f.as);
    const query = {
      kind: f.kind, scope: f.scope, fleet_id: f.fleet, author: f.author, include_inactive: f.all ? "true" : "",
      flagged: f.flagged ? "true" : "", limit, offset,
    };
    const res = await guarded(() => api("GET", "admin/entries", { query }));
    return res && { items: res.map(normaliseEntry), degraded: false };
  };

  const isSearch = (f) => Boolean(f.q) && !entryIdIn(f.q);
  const searchingAs = (f) => Boolean(f.as) && isSearch(f);
  const agentNamed = (name) => state.agents.find((a) => a.name === name) || { name, trust_level: 0, home_fleet_id: null };
  const hitRow = (item) => entryRow(item, { onOpen: (id) => openEntry(id, () => load(false)), fleetName });
  // A hit's row, followed by its score parts when the search explained itself.
  const hitRows = (item) => {
    const row = hitRow(item);
    if (!item.explain) return [row];
    const lines = explainLines(item.explain).map((line) => h("li", { text: line }));
    return [row, h("tr", { class: "explain-row" }, h("td", { colspan: 7 }, h("ul", { class: "explain-list small muted" }, lines)))];
  };

  // The agent view's extras: its banner, and what the admin finds that the agent does not.
  const showAsAgent = async (f, agentItems, mine) => {
    asNote.hidden = !f.as || isSearch(f);
    asBanner.hidden = !searchingAs(f);
    hiddenCard.hidden = true;
    if (!searchingAs(f)) return;
    const agent = agentNamed(f.as);
    asBanner.textContent = asAgentBanner(agent, fleetName);
    const adminPage = await searchPage(searchBody({ ...f, explain: "" }, PAGE, 0));
    if (!adminPage || mine !== ticket) return;
    const hidden = hiddenFrom(agent, adminPage.items, agentItems, fleetName);
    hiddenBody.replaceChildren(...hidden.map(({ hit, reason }) => {
      const row = hitRow(hit);
      row.append(h("td", { class: "small", text: reason }));
      return row;
    }));
    hiddenCard.querySelector(".hidden-title").textContent = `Hidden from ${agent.name}`;
    hiddenCard.hidden = hidden.length === 0;
  };

  const load = async (append) => {
    // Load more pages the filters the table shows, not unsent text in the form.
    const f = append && applied ? applied : filters();
    const mine = ++ticket;
    if (!append) { generation += 1; applied = f; asBanner.hidden = true; hiddenCard.hidden = true; }
    syncHash(f);
    more.disabled = true;
    try {
      if (!append) offset = 0;
      const page = await fetchItems(f, PAGE, offset);
      if (!page || mine !== ticket) return;
      degraded.hidden = !page.degraded;
      const items = page.items;
      if (!append) tbody.replaceChildren();
      offset += items.length;
      tbody.append(...items.flatMap(hitRows));
      const count = tbody.querySelectorAll("tr.clickable").length;
      const byId = Boolean(entryIdIn(f.q));
      if (count === 0) {
        const empty = byId ? "No memory has this id." : f.q ? "No memories match this search." : "No memories match these filters.";
        tbody.replaceChildren(h("tr", {}, h("td", { class: "empty", colspan: 7, text: empty })));
      }
      status.textContent = byId ? (count ? "The memory with this id (the filters do not apply to an id)" : "")
        : f.q ? `${count} result${count === 1 ? "" : "s"}, best match first` : `${count} shown, newest first`;
      moreWrap.hidden = items.length < PAGE;
      if (!append) await showAsAgent(f, items, mine);
    } catch (err) {
      if (mine === ticket) toast(err.message, true);
    } finally {
      more.disabled = false;
    }
  };
  const exporter = exportButton({
    fetchPage: async ({ limit, offset: at }) => {
      const page = await fetchItems({ ...(applied || filters()), explain: "" }, limit, at);
      return page && page.items;
    },
    columns: entryCsvColumns(fleetName),
    name: "memories",
    key: (r) => r.id,
    generation: () => generation,
  });

  form.addEventListener("submit", (event) => { event.preventDefault(); load(false); });
  for (const el of [fleet, kind, scope, stateSel, flagged, explain]) el.addEventListener("change", () => load(false));
  for (const el of [author, asAgent]) el.addEventListener("change", () => load(false));
  reset.addEventListener("click", () => {
    q.value = ""; asAgent.value = ""; fleet.value = ""; author.value = ""; kind.value = ""; scope.value = ""; stateSel.value = ""; flagged.checked = false; explain.checked = false;
    load(false);
  });
  more.addEventListener("click", () => load(true));

  fill(root, 
    pageHead("Memories", "What the agents have written. Search ranks like an agent's search (name an agent to see it as that agent does), a memory id finds that memory, and the list is newest first.", refreshButton()),
    card(null, h("div", {}, form), { className: "card-filters" }),
    asNote,
    degraded,
    asBanner,
    h("div", { class: "card" },
      h("div", { class: "card-body card-body-tight result-bar" }, status, exporter),
      h("div", { class: "table-wrap" }, h("table", { class: "entries" },
        h("thead", {}, h("tr", {}, ENTRY_HEADERS.map((t) => h("th", { text: t })))),
        tbody)),
      moreWrap),
    hiddenCard);
  await load(false);
}
