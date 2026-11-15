// Hivemind admin panel: agent dialogs, scout, and the Agents list (ADR 0029).
// Everything API-sourced is rendered with textContent (see core.js).

import {
  api, guarded, h, LEVELS, hooks, state, toast, openModal, closeModal, showKeyOnce, fleetName, fmtAgo, fmtTime, fmtNum, levelBadge, statusBadge, loadAgentsAndFleets, pageHead, refreshButton, table, card, link, fetchActivity, fetchMetrics, miniBar, parseHash, viewHref, unavailable, agentHref, ownerHref, optional, fill, fleetHref,
} from "./core.js";
import { cleanupDialog, cleanupsCard } from "./cleanup.js";
import { retireDialog, successionsCard } from "./retire.js";

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
      // Pick the new fleet, so a retry after a later failure reuses it instead of creating it again.
      select.insertBefore(h("option", { value: fleet.id, text: fleet.name }), select.lastElementChild);
      select.value = fleet.id;
      newName.value = "";
      sync();
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

/** Register and activate an agent for an owner who did not self-register (ADR 0120). */
function newAgentDialog(onDone) {
  const owner = h("input", { id: "f-owner", maxlength: 128, autocomplete: "off", placeholder: "e.g. jane.doe" });
  const name = h("input", { id: "f-name", maxlength: 63, autocomplete: "off", spellcheck: false, placeholder: "e.g. jane-laptop" });
  const level = levelSelect(1);
  const fleet = fleetPicker(null);
  openModal({
    title: "New agent",
    body: [
      h("p", { class: "muted", text: "For an owner who has not registered their agent themselves. This registers the agent and activates it at once; the key is shown once, so deliver it to the owner out of band." }),
      h("div", { class: "field" }, h("label", { for: "f-owner", text: "Owner alias" }), owner,
        h("span", { class: "hint", text: "Who the agent belongs to and who gets the key." })),
      h("div", { class: "field" }, h("label", { for: "f-name", text: "Agent name" }), name,
        h("span", { class: "hint", text: "Its identity in the hive: the author of everything it writes. Names are never reused." })),
      h("div", { class: "field" }, h("label", { for: "f-level", text: "Trust level" }), level),
      fleet.el,
    ],
    actions: [
      { label: "Cancel" },
      {
        label: "Create and issue key",
        class: "btn-primary",
        submit: true,
        onClick: async () => {
          const ownerAlias = owner.value.trim();
          const agentName = name.value.trim();
          if (!ownerAlias) throw new Error("Enter the owner alias.");
          if (!agentName) throw new Error("Enter the agent name.");
          const taken = state.agents.find((a) => a.name.toLowerCase() === agentName.toLowerCase());
          if (taken) throw new Error(`An agent named ${taken.name} already exists (${taken.status}). Use its Activate, Re-activate or Replace key instead, or pick another name.`);
          const homeFleetId = await fleet.resolve();
          const result = await guarded(() => api("POST", "admin/agents", {
            body: { name: agentName, owner_alias: ownerAlias, trust_level: Number(level.value), home_fleet_id: homeFleetId },
          }));
          if (!result) return;
          showKeyOnce(`Agent key for ${result.name}`, result.key,
            `Send it to ${result.owner_alias}. The agent presents it as its MCP key.`);
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

/** The agent's key was locked from the portal; the fix is Replace key. */
const lockedBadge = (agent) => h("span", {
  class: "badge badge-revoked",
  title: `Its key was locked from the agent portal${agent.key_locked_at ? ` ${fmtTime(agent.key_locked_at)}` : ""} (a leak was reported). It fails every request until you replace it.`,
  text: "key locked",
});

function replaceKeyDialog(agent, onDone) {
  openModal({
    title: `Replace the key of ${agent.name}?`,
    body: [
      agent.key_locked ? h("p", { class: "callout", text: "This key was locked from the agent portal after a leak was reported. Replacing it is how the agent gets back in." }) : null,
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

const SCOUT_NOTE = "A scout gets a one-line hint in hive_whoami to explore: try less-tested rivals, use explore search, re-check old entries and write what it tried. It reads and writes exactly as before.";

export const scoutBadge = (agent) => (agent.role === "scout"
  ? h("span", { class: "badge badge-level", title: "Marked as a scout", text: "Scout" })
  : null);

/** The agent page's Scout card: whether the agent is a scout, and the control to change that (ADR 0144). */
export function scoutCard(agent, onDone) {
  const url = `admin/agents/${encodeURIComponent(agent.name)}/role`;
  const isScout = agent.role === "scout";
  const confirm = ({ title, body, label, call, done }) => openModal({
    title,
    body: h("p", { text: body }),
    actions: [{ label: "Cancel" }, {
      label,
      class: "btn-primary",
      submit: true,
      onClick: async () => {
        await guarded(call);
        closeModal();
        toast(done);
        onDone();
      },
    }],
  });
  const button = isScout
    ? h("button", { type: "button", class: "btn", text: "Stop scouting", onclick: () => confirm({
      title: "Stop scouting?",
      body: `${agent.name} stops getting the scout hint in hive_whoami.`,
      label: "Stop scouting",
      call: () => api("DELETE", url),
      done: `${agent.name} is no longer a scout`,
    }) })
    : h("button", { type: "button", class: "btn btn-primary", text: "Make scout", disabled: agent.status === "revoked",
      title: agent.status === "revoked" ? "A revoked agent cannot be made a scout" : "", onclick: () => confirm({
        title: "Make a scout?",
        body: `${agent.name} gets a hint in hive_whoami to explore more. What it may read and write does not change.`,
        label: "Make scout",
        call: () => api("PUT", url, { body: { role: "scout" } }),
        done: `${agent.name} is now a scout`,
      }) });
  return card("Scout",
    h("div", {},
      h("div", { class: "row" }, isScout ? scoutBadge(agent) : h("span", { class: "muted", text: "Not a scout" }), button),
      h("p", { class: "muted small", text: agent.status === "revoked" ? `${SCOUT_NOTE} The role stays on the record and comes back if the agent is re-activated.` : SCOUT_NOTE })));
}

/** Fix the owner alias of any agent (ADR 0122): the Stats "users" tab groups by it. */
function ownerDialog(agent, onDone) {
  const owner = h("input", { id: "f-owner-edit", maxlength: 128, autocomplete: "off", value: agent.owner_alias || "", placeholder: "e.g. jane.doe" });
  openModal({
    title: `Owner of ${agent.name}`,
    body: [
      h("div", { class: "field" }, h("label", { for: "f-owner-edit", text: "Owner alias" }), owner,
        h("span", { class: "hint", text: "Fixes a typo or a changed alias. The agent and its memories stay as they are; only the grouping by owner (Stats, Agents) follows the new alias." })),
    ],
    actions: [
      { label: "Cancel" },
      {
        label: "Save",
        class: "btn-primary",
        submit: true,
        onClick: async () => {
          const ownerAlias = owner.value.trim();
          if (!ownerAlias) throw new Error("Enter the owner alias.");
          if (ownerAlias === (agent.owner_alias || "")) { closeModal(); return; }
          await guarded(() => api("PATCH", `admin/agents/${encodeURIComponent(agent.name)}`, { body: { owner_alias: ownerAlias } }));
          closeModal();
          toast(`${agent.name} now belongs to ${ownerAlias}`);
          onDone();
        },
      },
    ],
  });
  owner.select();
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
    buttons.push(h("button", { type: "button", class: "btn btn-danger btn-sm", text: "Retire…", title: "Hand its memories to a successor and revoke it", onclick: () => retireDialog(agent, refresh) }));
  } else if (agent.status === "revoked") {
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Clean up…", title: "Withdraw what this agent wrote in a time window and set its ratings aside", onclick: () => cleanupDialog(agent, refresh) }));
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Re-activate", onclick: () => activateDialog(agent, refresh) }));
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Retire…", title: "Hand its memories to a successor", onclick: () => retireDialog(agent, refresh) }));
  }
  buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Edit owner", title: "Correct the owner alias", onclick: () => ownerDialog(agent, refresh) }));
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

// --- agents ---------------------------------------------------------------------------------

export async function viewAgents(root) {
  const [, metrics, activity, cleanups, successions] = await Promise.all([
    loadAgentsAndFleets(), fetchMetrics(), fetchActivity(state.days),
    optional(() => api("GET", "admin/agent-cleanups")),
    optional(() => api("GET", "admin/successions")),
  ]);
  // ADR 0068: stale/wrong reports and how many a later supersession confirmed.
  const flagged = (metrics && metrics.feedback && metrics.feedback.by_agent) || {};
  const act = new Map(((activity && activity.agents) || []).map((a) => [a.name, a]));
  const maxWrites = Math.max(1, ...[...act.values()].map((a) => a.recent_writes || 0));
  const refresh = () => hooks.refresh();
  const search = h("input", { type: "search", placeholder: "Filter by name, owner or fleet…", "aria-label": "Filter agents" });
  // Filters live in the hash (#/agents?status=pending&activity=locked) so the Overview can link to them.
  const { params } = parseHash();
  const statusFilter = h("select", { "aria-label": "Status" }, ["all", "pending", "active", "revoked"].map((s) => h("option", { value: s, text: s === "all" ? "All statuses" : s })));
  if (["pending", "active", "revoked"].includes(params.get("status"))) statusFilter.value = params.get("status");
  // ADR 0110: the server says which agents are idle and for how many days.
  const idleDays = (state.agents[0] && state.agents[0].idle_after_days) || 30;
  const idleCount = state.agents.filter((a) => a.idle).length;
  const lockedCount = state.agents.filter((a) => a.key_locked && a.status === "active").length;
  const idleFilter = h("select", { "aria-label": "Activity" },
    h("option", { value: "all", text: "Any activity" }),
    h("option", { value: "locked", text: `Key locked (${lockedCount})` }),
    h("option", { value: "idle", text: `Idle ${idleDays}+ days (${idleCount})` }));
  if (["locked", "idle"].includes(params.get("activity"))) idleFilter.value = params.get("activity");
  const holder = h("div");
  const syncHash = () => {
    const next = viewHref("agents", {
      status: statusFilter.value === "all" ? "" : statusFilter.value, activity: idleFilter.value === "all" ? "" : idleFilter.value,
    });
    if (root.dataset.superseded) return; // the user navigated away while this view loaded
    if (window.location.hash !== next) history.replaceState(null, "", next);
  };
  const render = () => {
    const q = search.value.trim().toLowerCase();
    const shown = state.agents.filter((a) =>
      (statusFilter.value === "all" || a.status === statusFilter.value) &&
      (idleFilter.value === "all" || (idleFilter.value === "locked" ? a.key_locked && a.status === "active" : a.idle)) &&
      (!q || [a.name, a.owner_alias || "", fleetName(a.home_fleet_id)].some((v) => v.toLowerCase().includes(q))));
    holder.replaceChildren(table(
      ["Agent", "Status", "Trust level", "Home fleet", "Owner", "Entries", `Writes (${state.days}d)`, "Last write", "Flags confirmed", "Activated", "Last active", ""],
      shown.map((a) => {
        const x = act.get(a.name);
        const badge = scoutBadge(a);
        return h("tr", {},
          h("td", {}, link(a.name, agentHref(a.name), { class: "link strong" }), badge ? [" ", badge] : null),
          h("td", {}, statusBadge(a.status), a.key_locked && a.status === "active" ? [" ", lockedBadge(a)] : null),
          h("td", {}, a.status === "pending" ? h("span", { class: "muted", text: "—" }) : levelBadge(a.trust_level)),
          h("td", {}, a.home_fleet_id ? link(fleetName(a.home_fleet_id), fleetHref(a.home_fleet_id)) : "—"),
          h("td", {}, a.owner_alias ? link(a.owner_alias, ownerHref(a.owner_alias)) : "—"),
          h("td", { class: "num", title: x ? `${x.active} active` : "", text: x ? fmtNum(x.entries) : "—" }),
          h("td", { class: "num" }, x ? h("div", { class: "inline-bar" }, h("span", { text: fmtNum(x.recent_writes) }), miniBar(x.recent_writes || 0, maxWrites)) : "—"),
          h("td", { title: x ? fmtTime(x.last_write_at) : "", text: x && x.last_write_at ? fmtAgo(x.last_write_at) : "—" }),
          h("td", { title: "Stale or wrong reports later confirmed by a supersession", text: flagged[a.name] ? `${flagged[a.name].confirmed} of ${flagged[a.name].flags}` : "—" }),
          h("td", { title: fmtTime(a.activated_at), text: a.activated_at ? fmtAgo(a.activated_at) : "—" }),
          h("td", { title: a.last_used_at ? fmtTime(a.last_used_at) : "Its key has not been used since last-use tracking started", text: a.last_used_at ? fmtAgo(a.last_used_at) : (a.status === "active" ? "never" : "—") },
            a.idle ? [" ", h("span", { class: "badge badge-pending", title: `No request for ${idleDays}+ days`, text: "idle" })] : null),
          agentActions(a, refresh));
      }),
      state.agents.length ? "No agents match the filter." : "No agents have registered yet."));
  };
  search.addEventListener("input", render);
  statusFilter.addEventListener("change", () => { render(); syncHash(); });
  idleFilter.addEventListener("change", () => { render(); syncHash(); });
  render();
  fill(root, 
    pageHead("Agents", `${state.agents.length} registered`,
      h("button", { type: "button", class: "btn btn-primary btn-sm", text: "New agent", onclick: () => newAgentDialog(refresh) }),
      refreshButton()),
    card(null, holder, { head: [h("div", { class: "filters" }, search, statusFilter, idleFilter)], flush: true }),
    cleanupsCard(cleanups),
    successions && successions.length ? successionsCard(successions) : null,
    activity ? null : unavailable("Per-agent activity"));
}
