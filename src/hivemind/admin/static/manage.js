// Hivemind admin panel: agent management, fleets, audit log, org key (ADR 0029).
// Everything API-sourced is rendered with textContent (see core.js).

import {
  api, guarded, h, LEVELS, hooks, state, toast, openModal, closeModal, showKeyOnce, fleetName, fmtAgo, fmtTime, fmtNum,
  levelBadge, statusBadge, loadAgentsAndFleets, pageHead, refreshButton, table, card, link, memoriesHref,
  fetchActivity, fetchMetrics, miniBar, unavailable, agentHref, optional, fill,
} from "./core.js";
import { openEntry } from "./drawer.js";
import { PIN_HEADERS, pinRow } from "./lists.js";
import { cleanupDialog, cleanupsCard } from "./cleanup.js";
import { mergeDialog, mergesCard } from "./merge.js";

const AUDIT_ACTIONS = [
  "agent.register", "agent.activate", "agent.trust_level_set", "agent.home_fleet_set", "agent.revoke",
  "fleet.create", "fleet.rename", "fleet.merge", "fleet.unmerge", "org_key.rotate", "entry.withdraw", "entry.pin", "entry.unpin",
  "admin_key.issue", "admin_key.revoke", "agent_key.issue", "agent_key.replace",
  "agent.cleanup", "agent.cleanup_undo",
];
const AUDIT_PAGE = 50;

// --- shared agent actions --------------------------------------------------------

function levelSelect(selected) {
  return h("select", { id: "f-level" },
    LEVELS.map((l) => h("option", { value: l.value, selected: l.value === selected }, `${l.value} · ${l.name} — ${l.hint}`)));
}

/** Fleet picker with an inline "create a new fleet" option. */
function fleetPicker(selectedId) {
  const NEW = "__new__";
  const select = h("select", { id: "f-fleet" },
    state.fleets.length === 0 ? h("option", { value: "", text: "— no fleets yet —" }) : null,
    state.fleets.map((f) => h("option", { value: f.id, selected: f.id === selectedId, text: f.name })),
    h("option", { value: NEW, text: "+ Create a new fleet…" }));
  const newName = h("input", { id: "f-fleet-new", placeholder: "new fleet name", hidden: true });
  const sync = () => { newName.hidden = select.value !== NEW; if (!newName.hidden) newName.focus(); };
  select.addEventListener("change", sync);
  if (state.fleets.length === 0) { select.value = NEW; newName.hidden = false; }
  return {
    el: h("div", { class: "field" }, h("label", { for: "f-fleet", text: "Home fleet" }), select, newName,
      h("span", { class: "hint", text: "The fleet whose entries the agent reads and writes." })),
    async resolve() {
      if (select.value !== NEW) {
        if (!select.value) throw new Error("Choose a home fleet.");
        return select.value;
      }
      const name = newName.value.trim();
      if (!name) throw new Error("Enter a name for the new fleet.");
      const fleet = await api("POST", "admin/fleets", { body: { name } });
      state.fleets.push(fleet);
      toast(`Fleet “${fleet.name}” created`);
      return fleet.id;
    },
  };
}

function activateDialog(agent, onDone) {
  const reactivating = agent.status === "revoked";
  const level = levelSelect(reactivating ? agent.trust_level || 1 : 1);
  const fleet = fleetPicker(agent.home_fleet_id);
  openModal({
    title: reactivating ? `Re-activate ${agent.name}` : `Activate ${agent.name}`,
    body: [
      h("p", { class: "muted" },
        agent.owner_alias ? `Owner: ${agent.owner_alias}. ` : "No owner alias was given. ",
        "Activation issues the agent key, shown once — deliver it to the owner out of band."),
      h("div", { class: "field" }, h("label", { for: "f-level", text: "Trust level" }), level),
      fleet.el,
    ],
    actions: [
      { label: "Cancel" },
      {
        label: reactivating ? "Re-activate and issue key" : "Activate and issue key",
        class: "btn-primary",
        submit: true,
        onClick: async () => {
          const homeFleetId = await fleet.resolve();
          const result = await guarded(() => api("POST", `admin/agents/${encodeURIComponent(agent.name)}/activate`, {
            body: { trust_level: Number(level.value), home_fleet_id: homeFleetId },
          }));
          if (!result) return;
          showKeyOnce(`Agent key for ${agent.name}`, result.key,
            agent.owner_alias ? `Send it to ${agent.owner_alias}. The agent presents it as its MCP key.` : null);
          onDone();
        },
      },
    ],
  });
}

function revokeDialog(agent, onDone) {
  const rejecting = agent.status === "pending";
  openModal({
    title: rejecting ? `Reject ${agent.name}?` : `Revoke ${agent.name}?`,
    body: h("p", {},
      rejecting
        ? "The registration is rejected and the name stays reserved — it cannot be registered again. You can still activate it later."
        : "The agent's key stops working on its next request. The name stays reserved; re-activating issues a new key."),
    actions: [
      { label: "Cancel" },
      {
        label: rejecting ? "Reject" : "Revoke key",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          await guarded(() => api("POST", `admin/agents/${encodeURIComponent(agent.name)}/revoke`));
          closeModal();
          toast(rejecting ? `${agent.name} rejected` : `${agent.name} revoked`);
          onDone();
        },
      },
    ],
  });
}

function replaceKeyDialog(agent, onDone) {
  openModal({
    title: `Replace the key of ${agent.name}?`,
    body: [
      h("p", { class: "callout callout-danger", text: "The current key stops working on the agent's next request. The agent has to be given the new key before it can reach Hivemind again." }),
      h("p", { class: "muted small", text: "Its name, trust level, fleet and memories do not change." }),
    ],
    actions: [
      { label: "Cancel" },
      {
        label: "Replace key",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          const result = await guarded(() => api("POST", `admin/agents/${encodeURIComponent(agent.name)}/key`));
          if (!result) return;
          showKeyOnce(`New agent key for ${agent.name}`, result.key,
            "The previous key has stopped working. Give this one to the agent's owner now: it cannot be shown again.");
          onDone();
        },
      },
    ],
  });
}

function editDialog(agent, onDone) {
  const level = levelSelect(agent.trust_level);
  const fleet = fleetPicker(agent.home_fleet_id);
  openModal({
    title: `Edit ${agent.name}`,
    body: [
      h("div", { class: "field" }, h("label", { for: "f-level", text: "Trust level" }), level,
        h("span", { class: "hint", text: "Demoting to 0 makes the agent dormant: its key stays valid but it can do nothing." })),
      fleet.el,
      h("p", { class: "muted small", text: "Moving an agent never moves the entries it already wrote." }),
    ],
    actions: [
      { label: "Cancel" },
      {
        label: "Save",
        class: "btn-primary",
        submit: true,
        onClick: async () => {
          const body = {};
          if (Number(level.value) !== agent.trust_level) body.trust_level = Number(level.value);
          const fleetId = await fleet.resolve();
          if (fleetId !== agent.home_fleet_id) body.home_fleet_id = fleetId;
          if (Object.keys(body).length === 0) { closeModal(); return; }
          await guarded(() => api("PATCH", `admin/agents/${encodeURIComponent(agent.name)}`, { body }));
          closeModal();
          toast(`${agent.name} updated`);
          onDone();
        },
      },
    ],
  });
}

/** Rename a fleet. Its id never changes, so memories, pins and agents stay as they are. */
function renameFleetDialog(fleet, onDone) {
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

/** The buttons for what can be done to `agent` now (activate, edit, revoke…). */
export function agentButtons(agent, refresh) {
  const buttons = [];
  if (agent.status === "pending") {
    buttons.push(h("button", { type: "button", class: "btn btn-primary btn-sm", text: "Activate", onclick: () => activateDialog(agent, refresh) }));
    buttons.push(h("button", { type: "button", class: "btn btn-danger btn-sm", text: "Reject", onclick: () => revokeDialog(agent, refresh) }));
  } else if (agent.status === "active") {
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Edit", onclick: () => editDialog(agent, refresh) }));
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Replace key", onclick: () => replaceKeyDialog(agent, refresh) }));
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Clean up…", title: "Withdraw what this agent wrote in a time window and set its ratings aside", onclick: () => cleanupDialog(agent, refresh) }));
    buttons.push(h("button", { type: "button", class: "btn btn-danger btn-sm", text: "Revoke", onclick: () => revokeDialog(agent, refresh) }));
  } else if (agent.status === "revoked") {
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Clean up…", title: "Withdraw what this agent wrote in a time window and set its ratings aside", onclick: () => cleanupDialog(agent, refresh) }));
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Re-activate", onclick: () => activateDialog(agent, refresh) }));
  }
  return buttons;
}

export const agentActions = (agent, refresh) => h("td", { class: "actions" }, agentButtons(agent, refresh));

export function pendingQueue(refresh) {
  const pending = state.agents
    .filter((a) => a.status === "pending")
    .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
  const filter = h("input", { type: "search", placeholder: "Filter by name or owner…" });
  const tbody = h("tbody");
  const render = () => {
    const q = filter.value.trim().toLowerCase();
    const shown = pending.filter((a) => !q || a.name.toLowerCase().includes(q) || (a.owner_alias || "").toLowerCase().includes(q));
    // Group by owner alias: one owner often registers several agents.
    const groups = new Map();
    for (const agent of shown) {
      const owner = agent.owner_alias || "(no owner alias)";
      if (!groups.has(owner)) groups.set(owner, []);
      groups.get(owner).push(agent);
    }
    const rows = [];
    for (const [owner, agents] of groups) {
      rows.push(h("tr", { class: "group-row" }, h("td", { colspan: 3, text: `${owner} · ${agents.length} agent${agents.length === 1 ? "" : "s"}` })));
      for (const agent of agents) {
        rows.push(h("tr", {},
          h("td", {}, h("strong", { text: agent.name })),
          h("td", { title: fmtTime(agent.created_at), text: `registered ${fmtAgo(agent.created_at)}` }),
          agentActions(agent, refresh)));
      }
    }
    tbody.replaceChildren(...(rows.length ? rows : [h("tr", {}, h("td", { class: "empty", colspan: 3,
      text: pending.length ? "No pending agents match the filter." : "No agents are waiting for activation." }))]));
  };
  filter.addEventListener("input", render);
  render();
  return h("div", { class: "card" },
    h("div", { class: "card-head" }, h("h2", {}, "Waiting for activation ", h("span", { class: "badge badge-pending", text: pending.length })), filter),
    h("div", { class: "card-body" }, h("p", { class: "muted small", text: "Agents that registered with the org key and have not been issued an agent key yet, grouped by the owner they named." })),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { text: "Agent" }), h("th", { text: "Registered" }), h("th", {}))),
      tbody)));
}

export async function viewAudit(root) {
  await loadAgentsAndFleets().catch(() => undefined); // for fleet names only
  // A merged fleet is not in state.fleets, but audit rows still name it.
  const everyFleet = (await optional(() => api("GET", "admin/fleets", { query: { include_merged: "true" } }))) || [];
  const nameOf = (id) => {
    const fleet = everyFleet.find((f) => f.id === id);
    return fleet ? fleet.name : fleetName(id);
  };
  const actor = h("input", { type: "search", placeholder: "Actor (exact), e.g. admin:1a2b3c4d5e6f" });
  const action = h("select", {}, h("option", { value: "", text: "All actions" }), AUDIT_ACTIONS.map((a) => h("option", { value: a, text: a })));
  const since = h("input", { type: "datetime-local", title: "Since (local time)" });
  const apply = h("button", { type: "submit", class: "btn", text: "Apply" });
  const form = h("form", { class: "filters" }, actor, action, since, apply);
  const tbody = h("tbody");
  const more = h("button", { type: "button", class: "btn", text: "Load older" });
  const moreWrap = h("div", { class: "load-more" }, more);
  let cursor = null;

  // Fleet ids read better as names; the id stays in the tooltip.
  const target = (r) => {
    if (r.target && r.action === "fleet.create") {
      return h("td", { title: r.target, text: (r.detail && r.detail.name) || fleetName(r.target) });
    }
    if (r.target && r.action === "fleet.rename") {
      return h("td", { title: r.target, text: (r.detail && r.detail.to) || nameOf(r.target) });
    }
    if (r.target && (r.action === "fleet.merge" || r.action === "fleet.unmerge")) {
      return h("td", { title: r.target, text: nameOf(r.target) });
    }
    return h("td", { text: r.target ?? "—" });
  };
  const detail = (r) => {
    const d = { ...(r.detail || {}) };
    if (r.action === "fleet.create") delete d.name;
    if (d.home_fleet_id) {
      d.home_fleet = fleetName(d.home_fleet_id);
      delete d.home_fleet_id;
    }
    if (r.action === "agent.home_fleet_set") { d.from = fleetName(d.from); d.to = fleetName(d.to); }
    if (r.action === "fleet.merge" || r.action === "fleet.unmerge") {
      d.target_fleet = d.target_fleet_name || nameOf(d.target_fleet_id);
      delete d.target_fleet_id;
      delete d.target_fleet_name;
    }
    return Object.keys(d).length ? JSON.stringify(d) : "";
  };
  const actorBadge = (kind) => {
    if (kind === "cli") {
      return h("span", { class: "badge badge-muted", title: "hivemind-keys CLI: the actor name is typed by the operator and not verified", text: "cli · unverified" });
    }
    if (kind === "org_key") {
      return h("span", { class: "badge badge-muted", title: "a registration with the shared org key: the fingerprint names the key, not who used it", text: "org key" });
    }
    if (kind === "agent_key") {
      return h("span", { class: "badge badge-level", title: "an agent's own key, recorded by its verified name", text: "agent key" });
    }
    return h("span", { class: "badge badge-level", title: "a verified admin key, recorded by fingerprint", text: "admin key" });
  };
  const row = (r) => h("tr", {},
    h("td", { class: "nowrap", title: r.occurred_at, text: fmtTime(r.occurred_at) }),
    h("td", { class: "nowrap" }, actorBadge(r.actor_kind)),
    h("td", {}, h("code", { text: r.actor })),
    h("td", { text: r.action }),
    target(r),
    h("td", { class: "detail", text: detail(r) }));

  const load = async (append) => {
    const query = {
      actor: actor.value.trim(),
      action: action.value,
      since: since.value ? new Date(since.value).toISOString() : "",
      limit: AUDIT_PAGE,
      before: append ? cursor : "",
    };
    const rows = await guarded(() => api("GET", "admin/audit-log", { query }));
    if (!rows) return;
    if (!append) tbody.replaceChildren();
    tbody.append(...rows.map(row));
    if (!append && rows.length === 0) {
      tbody.append(h("tr", {}, h("td", { class: "empty", colspan: 6, text: "No audit rows match." })));
    }
    cursor = rows.length ? rows[rows.length - 1].id : cursor;
    moreWrap.hidden = rows.length < AUDIT_PAGE;
  };
  form.addEventListener("submit", (event) => { event.preventDefault(); load(false).catch((err) => toast(err.message, true)); });
  more.addEventListener("click", () => load(true).catch((err) => toast(err.message, true)));

  fill(root, 
    pageHead("Audit log", "Every admin action, newest first. CLI actors are claims typed by the operator, not verified identities."),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, form),
      h("div", { class: "card-body" }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["When", "Kind", "Actor", "Action", "Target", "Detail"].map((t) => h("th", { text: t })))),
        tbody)),
      moreWrap));
  await load(false);
}

export async function viewKeys(root) {
  const confirmInput = h("input", { placeholder: "type rotate to confirm", autocomplete: "off" });
  const rotate = h("button", { type: "submit", class: "btn btn-danger-solid", text: "Rotate the org key", disabled: true });
  confirmInput.addEventListener("input", () => { rotate.disabled = confirmInput.value.trim() !== "rotate"; });
  const form = h("form", { class: "row" }, confirmInput, rotate);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (confirmInput.value.trim() !== "rotate") return;
    rotate.disabled = true;
    try {
      const result = await guarded(() => api("POST", "admin/org-key/rotate"));
      if (result) {
        confirmInput.value = "";
        showKeyOnce("New org key", result.key, "Every previous org key has stopped working. Redistribute this one to everyone who registers agents.");
      }
    } catch (err) {
      toast(err.message, true);
    }
  });
  fill(root, 
    pageHead("Org key", "The single key shared by the whole cluster. It gates registration only."),
    h("div", { class: "card card-danger" },
      h("div", { class: "card-head" }, h("h2", { text: "Rotate the org key" })),
      h("div", { class: "card-body" },
        h("p", { class: "callout callout-danger", text: "Every existing org key stops working immediately, so nobody can register new agents with an old copy. Active agents are not affected: they use their own agent keys. To cut an agent off, revoke it." }),
        form)),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", { text: "Admin keys" })),
      h("div", { class: "card-body" },
        h("p", { class: "muted" }, "Admin keys are issued and revoked with the ", h("code", { text: "hivemind-keys" }),
          " CLI only (", h("code", { text: "issue-admin" }), " / ", h("code", { text: "revoke-admin" }),
          "), so a leaked admin key cannot mint more of itself."))));
}



// --- agents ---------------------------------------------------------------------------------

export async function viewAgents(root) {
  const [, metrics, activity, cleanups] = await Promise.all([
    loadAgentsAndFleets(), fetchMetrics(), fetchActivity(state.days),
    optional(() => api("GET", "admin/agent-cleanups")),
  ]);
  // ADR 0068: stale/wrong reports and how many a later supersession confirmed.
  const flagged = (metrics && metrics.feedback && metrics.feedback.by_agent) || {};
  const act = new Map(((activity && activity.agents) || []).map((a) => [a.name, a]));
  const maxWrites = Math.max(1, ...[...act.values()].map((a) => a.recent_writes || 0));
  const refresh = () => hooks.refresh();
  const search = h("input", { type: "search", placeholder: "Filter by name, owner or fleet…", "aria-label": "Filter agents" });
  const statusFilter = h("select", { "aria-label": "Status" }, ["all", "pending", "active", "revoked"].map((s) => h("option", { value: s, text: s === "all" ? "All statuses" : s })));
  const holder = h("div");
  const render = () => {
    const q = search.value.trim().toLowerCase();
    const shown = state.agents.filter((a) =>
      (statusFilter.value === "all" || a.status === statusFilter.value) &&
      (!q || [a.name, a.owner_alias || "", fleetName(a.home_fleet_id)].some((v) => v.toLowerCase().includes(q))));
    holder.replaceChildren(table(
      ["Agent", "Status", "Trust level", "Home fleet", "Owner", "Entries", `Writes (${state.days}d)`, "Last write", "Flags confirmed", "Activated", ""],
      shown.map((a) => {
        const x = act.get(a.name);
        return h("tr", {},
          h("td", {}, link(a.name, agentHref(a.name), { class: "link strong" })),
          h("td", {}, statusBadge(a.status)),
          h("td", {}, a.status === "pending" ? h("span", { class: "muted", text: "—" }) : levelBadge(a.trust_level)),
          h("td", { text: fleetName(a.home_fleet_id) }),
          h("td", { text: a.owner_alias || "—" }),
          h("td", { class: "num", title: x ? `${x.active} active` : "", text: x ? fmtNum(x.entries) : "—" }),
          h("td", { class: "num" }, x ? h("div", { class: "inline-bar" }, h("span", { text: fmtNum(x.recent_writes) }), miniBar(x.recent_writes || 0, maxWrites)) : "—"),
          h("td", { title: x ? fmtTime(x.last_write_at) : "", text: x && x.last_write_at ? fmtAgo(x.last_write_at) : "—" }),
          h("td", { title: "Stale or wrong reports later confirmed by a supersession", text: flagged[a.name] ? `${flagged[a.name].confirmed} of ${flagged[a.name].flags}` : "—" }),
          h("td", { title: fmtTime(a.activated_at), text: a.activated_at ? fmtAgo(a.activated_at) : "—" }),
          agentActions(a, refresh));
      }),
      state.agents.length ? "No agents match the filter." : "No agents have registered yet."));
  };
  search.addEventListener("input", render);
  statusFilter.addEventListener("change", render);
  render();
  fill(root, 
    pageHead("Agents", `${state.agents.length} registered`, refreshButton()),
    card(null, holder, { head: [h("div", { class: "filters" }, search, statusFilter)], flush: true }),
    cleanupsCard(cleanups),
    activity ? null : unavailable("Per-agent activity"));
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
        h("td", {}, h("strong", { text: f.name }), " ", h("span", { class: "badge badge-muted", title: fmtTime(f.merged_at), text: `merged into ${fleetName(f.merged_into)}` })),
        h("td", { colspan: 6, class: "muted", text: "Its memories, agents and pins moved to the fleet it was merged into." }),
        h("td", { title: fmtTime(f.created_at), text: fmtAgo(f.created_at) }),
        h("td", {}, h("code", { title: f.id, text: f.id.slice(0, 8) })),
        h("td", { class: "actions" }, renameButton(f)));
    }
    return h("tr", {},
      h("td", {}, x && x.entries ? link(f.name, memoriesHref({ fleet: f.id }), { class: "link strong" }) : h("strong", { text: f.name })),
      h("td", { class: "num", text: state.agents.filter((a) => a.status === "active" && a.home_fleet_id === f.id).length }),
      h("td", { class: "num", title: "Entries in this fleet: active of all", text: x ? `${fmtNum(x.active)} / ${fmtNum(x.entries)}` : (writes[f.name] ?? "—") }),
      h("td", { class: "num", text: x ? fmtNum(x.writers) : "—" }),
      h("td", { class: "num", text: x ? fmtNum(x.recent_writes) : "—" }),
      h("td", { title: x ? fmtTime(x.last_write_at) : "", text: x && x.last_write_at ? fmtAgo(x.last_write_at) : "—" }),
      h("td", { class: "num" }, pinCount(pins.get(f.id))),
      h("td", { title: fmtTime(f.created_at), text: fmtAgo(f.created_at) }),
      h("td", {}, h("code", { title: f.id, text: f.id.slice(0, 8) })),
      h("td", { class: "actions" }, renameButton(f), " ", h("button", {
        type: "button", class: "btn btn-sm", text: "Merge into…",
        title: "Move this fleet's memories and agents into another fleet",
        onclick: () => mergeDialog(f, () => hooks.refresh()),
      })));
  });
  const mergedToggle = h("input", { type: "checkbox", id: "show-merged" });
  mergedToggle.checked = showMerged;
  mergedToggle.addEventListener("change", () => { showMerged = mergedToggle.checked; hooks.refresh(); });
  fill(root, 
    pageHead("Fleets", "Fleets partition the pool. They cannot be deleted, but one can be renamed or merged into another.", refreshButton()),
    card("New fleet", form),
    card(null, table(
      ["Fleet", "Active agents", "Entries (active / all)", "Writers", `Writes (${state.days}d)`, "Last write", "Pins", "Created", "Id", ""],
      rows, "No fleets yet. Create one before activating agents.", { className: "fleets-table" }), {
      flush: true,
      head: [h("label", { class: "check", for: "show-merged" }, mergedToggle, " Show merged fleets")],
    }),
    mergesCard(merges),
    pinsCard(pins),
    nofleet ? h("p", { class: "muted small" }, `${fmtNum(nofleet.entries)} entries carry no fleet (${fmtNum(nofleet.active)} active).`) : null,
    activity ? null : unavailable("Per-fleet activity"));
}
