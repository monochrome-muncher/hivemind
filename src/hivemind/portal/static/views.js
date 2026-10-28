// Hivemind agent portal (ADR 0095): the views. Each lists what the agent's
// key can read through the portal routes, with the admin panel's shared
// tables (lists.js) and memory drawer (drawer.js).

import {
  api, apiRaw, h, state, guarded, parseHash, fleetName, pageHead, refreshButton, card, fill, table, toast, link, viewHref,
  KINDS, SCOPES, VERDICTS,
} from "./session.js";
import { openEntry } from "./drawer.js";
import { PIN_HEADERS, entryTable, feedbackTable, pinRow } from "./lists.js";

const select = (label, values, selected, anyText) => h("select", { "aria-label": label },
  h("option", { value: "", text: anyText }),
  values.map((v) => h("option", { value: v, text: v, selected: v === selected })));

/** Keep the view's hash in step with its filters (unless the user navigated away). */
function syncHash(root, view, filters) {
  if (root.dataset.superseded) return;
  const next = viewHref(view, filters);
  if (window.location.hash !== next) history.replaceState(null, "", next);
}

// --- its memories ----------------------------------------------------------------------

// What a successor also owns: the memories of the agents it took over from (ADR 0113).
const inherited = (me) => {
  const names = me.inherits_from || [];
  if (!names.length) return "";
  const who = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return `, plus what ${who} wrote before ${names.length === 1 ? "it was" : "they were"} retired`;
};

export async function viewMemories(root) {
  const me = state.me;
  const { params } = parseHash();
  const kind = select("Kind", KINDS, params.get("kind") || "", "Any kind");
  const stateSel = h("select", { "aria-label": "State" },
    h("option", { value: "", text: "Every state", selected: params.get("active") !== "1" }),
    h("option", { value: "1", text: "Active only", selected: params.get("active") === "1" }));
  const form = h("form", { class: "filters" }, kind, stateSel);

  let applied = null;
  const memories = entryTable({
    fetchPage: ({ limit, offset, fresh }) => {
      if (fresh || !applied) {
        applied = { kind: kind.value, active: stateSel.value };
        syncHash(root, "memories", applied);
      }
      return guarded(() => api("GET", "portal/entries", {
        query: { author: me.name, kind: applied.kind, include_inactive: applied.active ? "" : "true", limit, offset },
      }));
    },
    onOpen: (id) => openEntry(id, () => memories.load(true)),
    fleetName,
    emptyText: "It has not written any memories that match.",
    csvName: `${me.name}-memories`,
    statusText: (n) => `${n} shown, newest first`,
  });
  for (const el of [kind, stateSel]) el.addEventListener("change", () => memories.load(true));
  form.addEventListener("submit", (event) => { event.preventDefault(); memories.load(true); });

  fill(root,
    pageHead("Its memories", `Everything ${me.name} wrote${inherited(me)}, newest first, withdrawn and superseded ones included. Open one to read it in full or withdraw it.`, refreshButton()),
    card(null, form, { className: "card-filters" }),
    memories.el);
  await memories.load(true);
}

// --- verdicts it gave / verdicts on its memories ------------------------------------------

function feedbackView(direction) {
  return async (root) => {
    const me = state.me;
    const given = direction === "given";
    const view = given ? "given" : "received";
    const { params } = parseHash();
    const verdict = select("Verdict", VERDICTS, params.get("verdict") || "", "Any verdict");
    // One memory's verdicts (from the drawer's "all verdicts" link): a removable chip.
    let entry = given ? "" : params.get("entry") || "";
    const entryChip = h("span", { class: "chip chip-filter", hidden: !entry });
    const paintEntry = () => {
      entryChip.hidden = !entry;
      entryChip.replaceChildren(entry ? `One memory: ${entry.slice(0, 8)}…` : "", " ",
        h("button", { type: "button", class: "linklike", "aria-label": "Show every memory", text: "×", onclick: () => { entry = ""; paintEntry(); list.load(true); } }));
    };
    paintEntry();
    const form = h("form", { class: "filters" }, verdict, entryChip);

    let applied = null;
    const list = feedbackTable({
      fetchPage: ({ limit, offset, fresh }) => {
        if (fresh || !applied) {
          applied = { verdict: verdict.value, entry };
          syncHash(root, view, applied);
        }
        return guarded(() => api("GET", "portal/feedback", {
          query: { direction, verdict: applied.verdict, entry_id: applied.entry, limit, offset },
        }));
      },
      onOpen: (id) => openEntry(id, () => list.load(true)),
      fleetName,
      emptyText: given ? "No verdict it gave matches, on memories it can still read." : "No verdict on its memories matches.",
      csvName: `${me.name}-verdicts-${direction}`,
    });
    verdict.addEventListener("change", () => list.load(true));
    form.addEventListener("submit", (event) => { event.preventDefault(); list.load(true); });

    fill(root,
      pageHead(given ? "Verdicts it gave" : "Verdicts on its memories",
        given
          ? `Every helpful, stale or wrong verdict ${me.name} gave, newest first, on memories it can still read. A later verdict on the same memory replaces the earlier one.`
          : `What other agents said about ${me.name}'s memories${inherited(me)}, newest first. A stale or wrong verdict usually means the memory needs a new version.`,
        refreshButton()),
      card(null, form, { className: "card-filters" }),
      list.el,
      h("p", { class: "muted small note", text: "Notes are written by agents: shown as plain text, not checked by anyone." }));
    await list.load(true);
  };
}

export const viewGiven = feedbackView("given");
export const viewReceived = feedbackView("received");

// --- home fleet ---------------------------------------------------------------------------

export async function viewFleet(root) {
  const me = state.me;
  if (!me.home_fleet_id) {
    fill(root, pageHead("Home fleet", null),
      card(null, h("p", { class: "muted", text: `${me.name} has no home fleet yet. The admin sets one when it activates the agent.` })));
    return;
  }
  const pinsCard = h("div", {});
  const loadPins = async () => {
    const pins = await guarded(() => api("GET", "pins"));
    if (!pins) return;
    const open = (id) => openEntry(id, () => { loadPins(); memories.load(true); });
    pinsCard.replaceChildren(card(`Pinned (${pins.pins.length})`,
      table(PIN_HEADERS, pins.pins.map((p) => pinRow(p, { onOpen: open })), "Nothing is pinned in this fleet.", { className: "entries" }),
      { flush: true }));
  };
  const memories = entryTable({
    fetchPage: ({ limit, offset }) => guarded(() => api("GET", "portal/entries", {
      query: { fleet_id: me.home_fleet_id, scope: "fleet", limit, offset },
    })),
    onOpen: (id) => openEntry(id, () => memories.load(true)),
    fleetName,
    emptyText: "Nobody has written to this fleet yet.",
    csvName: `${fleetName(me.home_fleet_id)}-fleet-memories`,
  });

  fill(root,
    pageHead(fleetName(me.home_fleet_id), "The active memories of its home fleet, newest first, and what the fleet pinned for every member to read.", refreshButton()),
    pinsCard,
    h("section", { class: "agent-sec" }, h("div", { class: "section-head" }, h("h2", { text: "Fleet memories" })), memories.el));
  await Promise.all([loadPins().catch((err) => toast(err.message, true)), memories.load(true)]);
}

// --- search -------------------------------------------------------------------------------

export async function viewSearch(root) {
  const me = state.me;
  const { params } = parseHash();
  const get = (k) => params.get(k) || "";
  const q = h("input", { type: "search", value: get("q"), placeholder: "Search what it can read…", "aria-label": "Search" });
  const kind = select("Kind", KINDS, get("kind"), "Any kind");
  const scope = select("Scope", SCOPES, get("scope"), "Any scope");
  const author = h("input", { type: "text", value: get("author"), placeholder: "Author (agent name)", "aria-label": "Author" });
  const stateSel = h("select", { "aria-label": "State" },
    h("option", { value: "", text: "Active only", selected: get("all") !== "1" }),
    h("option", { value: "1", text: "Include inactive", selected: get("all") === "1" }));
  const go = h("button", { type: "submit", class: "btn btn-primary", text: "Search" });
  const form = h("form", { class: "filters filters-wide" }, q, kind, scope, author, stateSel, go);
  const degraded = h("p", { class: "callout callout-warn small", hidden: true, text: "The embedder is unavailable: results are keyword-only." });

  let applied = null;
  const results = entryTable({
    fetchPage: async ({ limit, offset, fresh }) => {
      if (fresh || !applied) {
        applied = { q: q.value.trim(), kind: kind.value, scope: scope.value, author: author.value.trim(), all: stateSel.value };
        syncHash(root, "search", applied);
      }
      const f = applied;
      if (!f.q) return [];
      const body = { query: f.q, limit, offset };
      if (f.kind) body.kind = f.kind;
      if (f.scope) body.scope = f.scope;
      if (f.author) body.author = f.author;
      if (f.all) body.include_inactive = true;
      const res = await guarded(() => apiRaw("POST", "portal/search", { body }));
      if (!res) return undefined;
      if (offset === 0) degraded.hidden = (res.headers.get("X-Hivemind-Degraded") || "") !== "keyword-only";
      return res.payload || [];
    },
    onOpen: (id) => openEntry(id, () => results.load(true)),
    fleetName,
    emptyText: "Type a search above, or nothing it can read matches.",
    csvName: `${me.name}-search`,
    statusText: (n) => (applied && applied.q ? `${n} result${n === 1 ? "" : "s"}, best match first` : ""),
  });
  form.addEventListener("submit", (event) => { event.preventDefault(); results.load(true); });

  fill(root,
    pageHead("Search", `Ranks like ${me.name}'s own search, over what its key can read. Searching here does not count in the fleet's search numbers.`,
      link("Its memories", viewHref("memories"), { class: "link small" })),
    card(null, form, { className: "card-filters" }),
    degraded,
    results.el);
  await results.load(true);
}
