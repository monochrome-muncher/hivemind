// Hivemind admin panel: fleet dialogs, phase, pins, and the Fleets list (ADR 0029).
// Everything API-sourced is rendered with textContent (see core.js).

import {
  api, guarded, h, hooks, state, toast, openModal, closeModal, fleetName, fmtAgo, fmtTime, fmtDate, fmtNum, loadAgentsAndFleets, pageHead, refreshButton, table, card, link, fetchActivity, fetchMetrics, unavailable, agentHref, optional, fill, fleetHref,
} from "./core.js";
import { openEntry } from "./drawer.js";
import { PIN_HEADERS, pinRow } from "./lists.js";
import { mergeDialog, mergesCard, moveDialog } from "./merge.js";

/** Rename a fleet. Its id never changes, so memories, pins and agents stay as they are. */
export function renameFleetDialog(fleet, onDone) {
  const input = h("input", { id: "f-fleet-rename", value: fleet.name, maxlength: 200, required: true, autocomplete: "off" });
  openModal({
    title: `Rename ${fleet.name}`,
    body: [
      h("div", { class: "field" }, h("label", { for: "f-fleet-rename", text: "New name" }), input,
        h("span", { class: "hint", text: "Names are unique. Memories, pins and agents stay with the fleet." })),
    ],
    actions: [
      { label: "Cancel" },
      {
        label: "Rename",
        class: "btn-primary",
        submit: true,
        onClick: async () => {
          const name = input.value.trim();
          if (!name) throw new Error("Enter a name for the fleet.");
          if (name === fleet.name) { closeModal(); return; }
          await guarded(() => api("PATCH", `admin/fleets/${encodeURIComponent(fleet.id)}`, { body: { name } }));
          closeModal();
          toast(`Fleet renamed to “${name}”`);
          onDone();
        },
      },
    ],
  });
  input.select();
}

const PHASES = [{ value: "explore", label: "Explore" }, { value: "consolidate", label: "Consolidate" }];
const DEFAULT_PHASE_DAYS = 14;
const MAX_PHASE_DAYS = 30;
const DAY_MS = 24 * 60 * 60 * 1000;

const phaseLabel = (phase) => (PHASES.find((p) => p.value === phase) || { label: phase }).label;

/** The phase in force as "Explore until 2026-12-01"; a fleet with none shows a dash (ADR 0143). */
export function phaseSummary(fleet) {
  return fleet.phase
    ? h("span", { title: fmtTime(fleet.phase_until), text: `${phaseLabel(fleet.phase)} until ${fmtDate(fleet.phase_until)}` })
    : h("span", { class: "muted", text: "—" });
}

/** The fleet page's phase card: what is in force and the control to set or clear it (ADR 0143). */
export function fleetPhaseCard(fleet, onDone) {
  const url = `admin/fleets/${encodeURIComponent(fleet.id)}/phase`;
  const select = h("select", { id: "f-phase", "aria-label": "Phase" },
    PHASES.map((p) => h("option", { value: p.value, selected: p.value === fleet.phase, text: p.label })));
  const days = h("input", { id: "f-phase-days", class: "phase-days", type: "number", min: 1, max: MAX_PHASE_DAYS, step: 1, value: DEFAULT_PHASE_DAYS, "aria-label": "Days" });
  const confirm = ({ title, body, label, onClick }) => openModal({
    title,
    body: h("p", { text: body }),
    actions: [{ label: "Cancel" }, { label, class: "btn-primary", submit: true, onClick }],
  });
  const set = async () => {
    const count = Number(days.value);
    if (!Number.isInteger(count) || count < 1 || count > MAX_PHASE_DAYS) throw new Error(`Days must be a whole number from 1 to ${MAX_PHASE_DAYS}.`);
    const until = new Date(Date.now() + count * DAY_MS).toISOString();
    await guarded(() => api("PUT", url, { body: { phase: select.value, until } }));
    closeModal();
    toast(`${phaseLabel(select.value)} phase set for ${count} ${count === 1 ? "day" : "days"}`);
    onDone();
  };
  const onSet = () => {
    if (!fleet.phase) return set().catch((err) => toast(err.message, true));
    return confirm({
      title: "Replace the current phase?",
      body: `${fleet.name} is in its ${phaseLabel(fleet.phase).toLowerCase()} phase until ${fmtDate(fleet.phase_until)}. The new phase replaces it.`,
      label: "Replace",
      onClick: set,
    });
  };
  const onClear = () => confirm({
    title: "Clear the phase?",
    body: `${fleet.name} goes back to no phase. Its agents stop seeing the hint.`,
    label: "Clear phase",
    onClick: async () => {
      await guarded(() => api("DELETE", url));
      closeModal();
      toast("Phase cleared");
      onDone();
    },
  });
  const clear = h("button", { type: "button", class: "btn", text: "Clear", disabled: !fleet.phase, onclick: onClear });
  return card("Phase",
    h("div", {},
      h("p", {}, "In force: ", phaseSummary(fleet)),
      h("div", { class: "row" },
        select, days, h("span", { class: "muted", text: "days" }),
        h("button", { type: "button", class: "btn btn-primary", text: "Set", onclick: onSet }), clear),
      h("p", { class: "muted small", text: "Agents in this fleet see a one-line hint in hive_whoami and hive_pinned until the phase ends. Ranking is unchanged. The end date is in UTC." })));
}

// --- fleets ---------------------------------------------------------------------------------

const pinCount = (list) => {
  if (!list) return h("span", { class: "muted", text: "—" });
  const changed = list.filter((p) => p.changed_since_pin).length;
  return h("span", {}, fmtNum(list.length),
    changed ? [" ", h("span", { class: "badge badge-pending", title: "Pins superseded or flagged since they were pinned", text: `${changed} to review` })] : null);
};

/** The pinned memories of one fleet, with a fleet picker. `pins` maps fleet id to its pins (or null). */
function pinsCard(pins) {
  // Fleets whose pins need attention (changed) come first.
  const attention = (f) => pins.get(f.id).filter((p) => p.changed_since_pin).length;
  const withPins = state.fleets.filter((f) => pins.get(f.id) && pins.get(f.id).length)
    .sort((a, b) => attention(b) - attention(a));
  if (!withPins.length) {
    return card("Pinned memories", h("p", { class: "muted", text: "No fleet has pinned anything. Agents pin a fleet's must-read memories (up to 10 per fleet)." }));
  }
  const picker = h("select", { "aria-label": "Fleet" }, withPins.map((f) => h("option", { value: f.id, text: `${f.name} · ${pins.get(f.id).length}` })));
  const holder = h("div");
  const render = () => holder.replaceChildren(table(PIN_HEADERS,
    pins.get(picker.value).map((p) => pinRow(p, { onOpen: (id) => openEntry(id, () => hooks.refresh()), agentHref })), ""));
  picker.addEventListener("change", render);
  render();
  return card("Pinned memories", holder, {
    head: [picker], flush: true,
  });
}

let showMerged = false; // the Fleets view's "Show merged fleets" filter (ADR 0107)

export async function viewFleets(root) {
  const [, metrics, activity, merges, everyFleet] = await Promise.all([
    loadAgentsAndFleets(), fetchMetrics(), fetchActivity(state.days),
    optional(() => api("GET", "admin/fleet-merges")),
    showMerged ? optional(() => api("GET", "admin/fleets", { query: { include_merged: "true" } })) : null,
  ]);
  // One read per fleet: fleets are few, and a fleet holds at most 10 pins.
  const pinLists = await Promise.all(state.fleets.map((f) =>
    optional(() => api("GET", `admin/fleets/${encodeURIComponent(f.id)}/pins`)).then((r) => [f.id, r ? r.pins : null])));
  const pins = new Map(pinLists);
  const writes = (metrics && metrics.fleets.writes_by_fleet) || {};
  const act = new Map(((activity && activity.fleets) || []).filter((f) => f.fleet_id).map((f) => [f.fleet_id, f]));
  const nofleet = ((activity && activity.fleets) || []).find((f) => !f.fleet_id);
  const name = h("input", { placeholder: "New fleet name", required: true, "aria-label": "New fleet name" });
  const create = h("button", { type: "submit", class: "btn btn-primary", text: "Create fleet" });
  const form = h("form", { class: "row" }, name, create);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = name.value.trim();
    if (!value) return;
    create.disabled = true;
    try {
      const fleet = await guarded(() => api("POST", "admin/fleets", { body: { name: value } }));
      if (fleet) { toast(`Fleet “${fleet.name}” created`); hooks.refresh(); }
    } catch (err) {
      toast(err.message, true);
    } finally {
      create.disabled = false;
    }
  });
  const renameButton = (f) => h("button", {
    type: "button", class: "btn btn-sm", text: "Rename",
    title: "Give this fleet a new name; its memories, pins and agents stay with it",
    onclick: () => renameFleetDialog(f, () => hooks.refresh()),
  });
  const rowFleets = showMerged && everyFleet ? everyFleet : state.fleets;
  const rows = rowFleets.map((f) => {
    const x = act.get(f.id);
    if (f.merged_into) {
      return h("tr", { class: "merged-fleet" },
        h("td", {}, link(f.name, fleetHref(f.id), { class: "link strong" }), " ", h("span", { class: "badge badge-muted", title: fmtTime(f.merged_at), text: `merged into ${fleetName(f.merged_into)}` })),
        h("td", { colspan: 7, class: "muted", text: "Its memories, agents and pins moved to the fleet it was merged into." }),
        h("td", { title: fmtTime(f.created_at), text: fmtAgo(f.created_at) }),
        h("td", {}, h("code", { title: f.id, text: f.id.slice(0, 8) })),
        h("td", { class: "actions" }, renameButton(f)));
    }
    return h("tr", {},
      h("td", {}, link(f.name, fleetHref(f.id), { class: "link strong" })),
      h("td", { class: "num", text: state.agents.filter((a) => a.status === "active" && a.home_fleet_id === f.id).length }),
      h("td", { class: "num", title: "Entries in this fleet: active of all", text: x ? `${fmtNum(x.active)} / ${fmtNum(x.entries)}` : (writes[f.name] ?? "—") }),
      h("td", { class: "num", text: x ? fmtNum(x.writers) : "—" }),
      h("td", { class: "num", text: x ? fmtNum(x.recent_writes) : "—" }),
      h("td", { title: x ? fmtTime(x.last_write_at) : "", text: x && x.last_write_at ? fmtAgo(x.last_write_at) : "—" }),
      h("td", { class: "num" }, pinCount(pins.get(f.id))),
      h("td", {}, phaseSummary(f)),
      h("td", { title: fmtTime(f.created_at), text: fmtAgo(f.created_at) }),
      h("td", {}, h("code", { title: f.id, text: f.id.slice(0, 8) })),
      h("td", { class: "actions" }, renameButton(f), " ",
        h("button", {
          type: "button", class: "btn btn-sm", text: "Move agents…",
          title: "Move some of this fleet's agents, with the fleet memories they wrote, to another fleet",
          onclick: () => moveDialog(f, () => hooks.refresh()),
        }), " ",
        h("button", {
          type: "button", class: "btn btn-sm", text: "Merge into…",
          title: "Move this fleet's memories and agents into another fleet",
          onclick: () => mergeDialog(f, () => hooks.refresh()),
        })));
  });
  const mergedToggle = h("input", { type: "checkbox", id: "show-merged" });
  mergedToggle.checked = showMerged;
  mergedToggle.addEventListener("change", () => { showMerged = mergedToggle.checked; hooks.refresh(); });
  fill(root, 
    pageHead("Fleets", "Fleets partition the pool. They cannot be deleted, but one can be renamed, merged into another, or split by moving some agents out.", refreshButton()),
    card("New fleet", form),
    card(null, table(
      ["Fleet", "Active agents", "Entries (active / all)", "Writers", `Writes (${state.days}d)`, "Last write", "Pins", "Phase", "Created", "Id", ""],
      rows, "No fleets yet. Create one before activating agents.", { className: "fleets-table" }), {
      flush: true,
      head: [h("label", { class: "check", for: "show-merged" }, mergedToggle, " Show merged fleets")],
    }),
    mergesCard(merges),
    pinsCard(pins),
    nofleet ? h("p", { class: "muted small" }, `${fmtNum(nofleet.entries)} entries carry no fleet (${fmtNum(nofleet.active)} active).`) : null,
    activity ? null : unavailable("Per-fleet activity"));
}
