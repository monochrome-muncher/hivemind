// Hivemind admin panel: one owner's page (#/owner?alias=<alias>).
//
// An owner is the alias agents registered under, matched exactly as stored. The page groups that
// owner's agents and offers the one-step offboarding (ADR 0135). The alias is self-reported, so
// the page says so; agents without an alias have no owner page.

import {
  api, h, state, loadAgentsAndFleets, fetchActivity, fleetName, pageHead, refreshButton, card, stat, table, statusBadge, levelBadge,
  fmtNum, fmtAgo, fmtTime, link, memoriesHref, agentHref, hooks, windowSelector, fill, optional, parseHash,
} from "./core.js";
import { offboardDialog } from "./offboard.js";

function agentRow(agent, row, successors) {
  const heir = successors.get(agent.name);
  return h("tr", {},
    h("td", {}, link(agent.name, agentHref(agent.name), { class: "link strong" })),
    h("td", {}, statusBadge(agent.status), heir ? [" ", h("span", { class: "badge badge-muted", text: `retired to ${heir}` })] : null),
    h("td", {}, agent.status === "pending" ? h("span", { class: "muted", text: "—" }) : levelBadge(agent.trust_level)),
    h("td", { text: fleetName(agent.home_fleet_id) }),
    h("td", { class: "num", title: row ? `${row.active} active` : "", text: row ? `${fmtNum(row.entries)} / ${fmtNum(row.active)}` : "—" }),
    h("td", { class: "num", text: row ? fmtNum(row.recent_writes) : "—" }),
    h("td", { title: agent.last_used_at ? fmtTime(agent.last_used_at) : "", text: agent.last_used_at ? fmtAgo(agent.last_used_at) : "—" }),
    h("td", {}, row && row.entries ? link("Memories", memoriesHref({ author: agent.name, all: "1" }), { class: "link small" }) : h("span", { class: "muted", text: "—" })));
}

function numbers(agents, rows, days) {
  const sum = (key) => rows.reduce((total, r) => total + (r[key] || 0), 0);
  return h("div", { class: "stats" },
    stat("Agents", fmtNum(agents.length)),
    stat("Active agents", fmtNum(agents.filter((a) => a.status === "active").length)),
    stat("Memories written", fmtNum(sum("entries")), { hint: `${fmtNum(sum("active"))} still active` }),
    stat("Verdicts given", fmtNum(sum("verdicts_given")), { hint: `${fmtNum(sum("recent_verdicts"))} in ${days}d` }));
}

export async function viewOwner(root) {
  const alias = parseHash().params.get("alias") || "";
  const back = link("All agents", "#/agents", { class: "link small" });
  if (!alias) {
    fill(root, pageHead("Owner", null, back), card(null, h("p", { class: "muted", text: "No owner named. Open one from the Stats page (Users) or an agent." })));
    return;
  }
  const [, activity, successions] = await Promise.all([
    loadAgentsAndFleets(), fetchActivity(state.days), optional(() => api("GET", "admin/successions")),
  ]);
  const agents = state.agents.filter((a) => a.owner_alias === alias).sort((a, b) => a.name.localeCompare(b.name));
  const byName = new Map(((activity && activity.agents) || []).map((a) => [a.name, a]));
  const successors = new Map((successions || []).map((s) => [s.retired_agent, s.successor]));
  const offboard = h("button", {
    type: "button", class: "btn btn-danger btn-sm", text: "Offboard this owner…",
    title: "Retire or revoke all of this owner's agents in one step", onclick: () => offboardDialog(alias, () => hooks.refresh()),
  });
  fill(root,
    pageHead(alias, "Owner alias: a label agents gave when registering, not a verified identity.",
      back, windowSelector(() => hooks.refresh()), refreshButton(), agents.length ? offboard : null),
    agents.length
      ? [numbers(agents, agents.map((a) => byName.get(a.name) || {}), state.days),
        card("Agents", table(["Agent", "Status", "Trust level", "Home fleet", "Entries / active", `Writes (${state.days}d)`, "Last used", "Memories"],
          agents.map((a) => agentRow(a, byName.get(a.name), successors)), "No agents."), { flush: true })]
      : card(null, h("p", { class: "muted", text: `No registered agent has the owner alias “${alias}”. Aliases match exactly, capitals included.` })));
}
