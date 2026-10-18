// Hivemind admin panel: the Feedback log (who rated what).
// Every verdict, newest first, each with the memory it is about, from
// GET admin/feedback. Filters live in the hash query so other views can
// deep-link: #/feedback?reporter=bob&verdict=wrong&author=alice&fleet=<id>&entry=<id>.
//
// The table itself is lists.js's feedbackTable(), shared with any other
// front end; this module only adds the admin filters and route.

import {
  api, h, state, guarded, parseHash, loadAgentsAndFleets, fleetName, pageHead, refreshButton, card, link, memoriesHref, agentHref, VERDICTS, fill,
} from "./core.js";
import { openEntry } from "./drawer.js";
import { feedbackTable } from "./lists.js";

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
    fleetName,
    agentHref,
  });

  form.addEventListener("submit", (event) => { event.preventDefault(); table.load(true); });
  for (const el of [verdict, fleet, reporter, author]) el.addEventListener("change", () => table.load(true));
  reset.addEventListener("click", () => {
    reporter.value = ""; verdict.value = ""; author.value = ""; fleet.value = ""; entry = ""; paintEntry();
    table.load(true);
  });

  fill(root, 
    pageHead("Feedback", "Who rated which memory, newest first. A reporter's latest verdict on a memory replaces its earlier one.",
      link("Memories", memoriesHref(), { class: "link small" }), refreshButton()),
    card(null, h("div", {}, form), { className: "card-filters" }),
    table.el,
    h("p", { class: "muted small note", text: "Verdicts from revoked agents are left out, as in every count. Notes are written by agents: shown as plain text, not checked by anyone." }));
  await table.load(true);
}
