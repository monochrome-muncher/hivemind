// Hivemind admin panel: one fleet's page (#/fleet?id=<fleet id>).
//
// Everything about one fleet in one place: its activity, members, pins, memories, flag backlog
// and history. The rename, move and merge dialogs are the Fleets view's. A merged
// fleet keeps its page and says where it went. Sections load side by side; one that fails says
// so and leaves the others alone.

import {
  api, h, state, guarded, parseHash, loadAgentsAndFleets, fetchActivity, fleetName, pageHead, refreshButton, card, stat, table,
  statusBadge, levelBadge, fmtNum, fmtAgo, fmtTime, fmtDate, link, memoriesHref, feedbackHref, agentHref, ownerHref, hooks, windowSelector,
  fill, optional, auditHref, viewHref, fleetHref, unavailable,
} from "./core.js";
import { openEntry } from "./drawer.js";
import { cursorPages, entryTable, flaggedTable, pagedTable, pinRow, PIN_HEADERS } from "./lists.js";
import { AUDIT_HEADERS, auditCsvColumns, auditRow, fleetPhaseCard, renameFleetDialog } from "./manage.js";
import { mergeDialog, mergesCard, moveDialog } from "./merge.js";
import { membersOf, recordsForFleet } from "./fleetdata.js";

const back = () => link("All fleets", "#/fleets", { class: "link small" });

/** A card that shows "Loading…" until `load()` resolves, then `render(result)`; a failure shows its message in the card. */
function loadedCard(title, load, render) {
  const body = h("div", { class: "card-body" }, h("p", { class: "muted", text: "Loading…" }));
  const holder = h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: title })), body);
  const done = load().then(
    (result) => { body.replaceChildren(render(result)); },
    (err) => { body.replaceChildren(h("p", { class: "form-error", text: `Could not load this section: ${err.message}` })); });
  return { el: holder, done };
}

function header(fleet, members) {
  const merged = Boolean(fleet.merged_into);
  const pairs = [
    ["Id", h("code", { text: fleet.id })],
    ["Created", h("span", { title: fmtTime(fleet.created_at), text: fmtDate(fleet.created_at) })],
    ["Members", `${fmtNum(members.length)} ${members.length === 1 ? "agent" : "agents"}, ${fmtNum(members.filter((a) => a.status === "active").length)} active`],
    merged ? ["Merged into", h("span", {}, link(fleetName(fleet.merged_into), fleetHref(fleet.merged_into)), ` ${fmtAgo(fleet.merged_at)}`)] : null,
  ].filter(Boolean);
  const buttons = merged ? [] : [
    h("button", { type: "button", class: "btn btn-sm", text: "Rename", onclick: () => renameFleetDialog(fleet, () => hooks.refresh()) }),
    h("button", { type: "button", class: "btn btn-sm", text: "Move agents…", onclick: () => moveDialog(fleet, () => hooks.refresh()) }),
    h("button", { type: "button", class: "btn btn-sm", text: "Merge into…", onclick: () => mergeDialog(fleet, () => hooks.refresh()) }),
  ];
  return card("Fleet", h("dl", { class: "meta" }, pairs.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)])),
    { head: [h("div", { class: "row tight" }, buttons)] });
}

function numbers(row, days) {
  return h("div", { class: "stats" },
    stat("Memories", row ? fmtNum(row.entries) : "—", { hint: row ? `${fmtNum(row.active)} still active` : null }),
    stat(`Writes, ${days}d`, row ? fmtNum(row.recent_writes) : "—", { hint: row && row.last_write_at ? `last ${fmtAgo(row.last_write_at)}` : null }),
    stat("Writers", row ? fmtNum(row.writers) : "—"));
}

function memberRow(agent) {
  return h("tr", {},
    h("td", {}, link(agent.name, agentHref(agent.name), { class: "link strong" })),
    h("td", {}, agent.owner_alias ? link(agent.owner_alias, ownerHref(agent.owner_alias)) : h("span", { class: "muted", text: "—" })),
    h("td", {}, statusBadge(agent.status)),
    h("td", {}, agent.status === "pending" ? h("span", { class: "muted", text: "—" }) : levelBadge(agent.trust_level)),
    h("td", { title: agent.last_used_at ? fmtTime(agent.last_used_at) : "", text: agent.last_used_at ? fmtAgo(agent.last_used_at) : "—" }));
}

const membersCard = (members) => card("Members", table(["Agent", "Owner", "Status", "Trust level", "Key last used"], members.map(memberRow), "No agent has this as its home fleet."), { flush: true });

export async function viewFleet(root) {
  const id = parseHash().params.get("id") || "";
  if (!id) {
    fill(root, pageHead("Fleet", null, back()), card(null, h("p", { class: "muted", text: "No fleet named. Open one from the Fleets page." })));
    return;
  }
  const [, activity, everyFleet, allMerges] = await Promise.all([
    loadAgentsAndFleets(), fetchActivity(state.days), optional(() => api("GET", "admin/fleets", { query: { include_merged: "true" } })),
    optional(() => api("GET", "admin/fleet-merges")),
  ]);
  const fleet = (everyFleet || state.fleets).find((f) => f.id === id);
  if (!fleet) {
    fill(root, pageHead("Fleet", null, back()), card(null, h("p", { class: "muted", text: "No such fleet. It may have been mistyped, or the link is old." })));
    return;
  }
  const nameOf = (fleetId) => {
    const known = (everyFleet || []).find((f) => f.id === fleetId);
    return known ? known.name : fleetName(fleetId);
  };
  const members = membersOf(state.agents, id);
  const merged = Boolean(fleet.merged_into);
  const row = activity ? (activity.fleets || []).find((f) => f.fleet_id === id) || null : null;

  const pins = loadedCard("Pinned memories", () => guarded(() => api("GET", `admin/fleets/${encodeURIComponent(id)}/pins`)),
    (result) => (result && result.pins.length
      ? table(PIN_HEADERS, result.pins.map((p) => pinRow(p, { onOpen: (entryId) => openEntry(entryId, () => hooks.refresh()), agentHref })), "")
      : h("p", { class: "muted", text: "Nothing is pinned. Agents pin a fleet's must-read memories, up to 10." })));

  const memories = entryTable({
    fetchPage: ({ limit, offset }) => guarded(() => api("GET", "admin/entries", { query: { fleet_id: id, include_inactive: "true", limit, offset } })),
    onOpen: (entryId) => openEntry(entryId, () => memories.load(true)),
    fleetName,
    emptyText: "The fleet has no memories.",
    csvName: `fleet-${fleet.name}-memories`,
    statusText: (n) => `${n} shown, newest first, withdrawn and superseded included`,
  });
  const flagged = flaggedTable({
    fetchPage: ({ limit, offset }) => guarded(() => api("GET", "admin/flagged", { query: { fleet_id: id, limit, offset } })),
    onOpen: (entryId) => openEntry(entryId, () => flagged.load(true)),
    fleetName,
    agentHref,
    emptyText: "No memory of this fleet is flagged.",
    csvName: `fleet-${fleet.name}-flagged`,
  });
  const pages = () => cursorPages(({ limit, before }) => guarded(() => api("GET", "admin/audit-log", { query: { target: id, limit, before } })));
  const audit = pagedTable({
    headers: AUDIT_HEADERS,
    className: "entries audit-log",
    fetchPage: pages(),
    renderRow: auditRow(nameOf),
    emptyText: "Nothing has been recorded about the fleet.",
    statusText: (n) => `${n} shown, newest first`,
    csv: { columns: auditCsvColumns, name: `fleet-${fleet.name}-audit`, key: (r) => r.id, fetchPage: pages() },
  });

  const section = (title, body, href) => h("section", { class: "agent-sec" },
    h("div", { class: "section-head" }, h("h2", { text: title }), href ? link("Open with filters", href, { class: "link small" }) : null),
    body);

  fill(root,
    pageHead(fleet.name, "Fleet page: who is in it, what it holds and what was done to it.", back(), windowSelector(() => hooks.refresh()), refreshButton()),
    numbers(row, state.days),
    header(fleet, members),
    merged ? null : fleetPhaseCard(fleet, () => hooks.refresh()),
    membersCard(members),
    pins.el,
    section("Memories", memories.el, memoriesHref({ fleet: id, all: "1" })),
    section("Flag backlog", flagged.el, viewHref("health", { fleet: id })),
    mergesCard(allMerges && recordsForFleet(allMerges, id)) || card("Merges and moves", unavailable("The merge list")),
    section("Audit log", audit.el, auditHref({ target: id })));
  await Promise.all([pins.done, memories.load(true), flagged.load(true), audit.load(true)]);
}
