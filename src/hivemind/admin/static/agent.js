// Hivemind admin panel: one agent's page (#/agent?name=<name>).
//
// Everything about one agent in one place: its registration, its numbers,
// the memories it wrote, the verdicts it gave and the verdicts its memories
// got. A name that wrote or rated but is not registered (the admin key, a
// legacy user) gets the same page without the registration part.
//
// The three lists are lists.js tables over admin routes. The agent portal
// shows the same lists for its own agent over the portal routes.

import {
  api, h, state, guarded, parseHash, loadAgentsAndFleets, fetchMetrics, fetchActivity, fleetName, pageHead, refreshButton,
  card, stat, statusBadge, levelBadge, fmtNum, fmtAgo, fmtTime, link, memoriesHref, feedbackHref, agentHref, hooks,
  windowSelector, fill,
} from "./core.js";
import { openEntry } from "./drawer.js";
import { entryTable, feedbackTable } from "./lists.js";
import { agentButtons } from "./manage.js";

function profile(agent) {
  if (!agent) {
    return card("Registration", h("p", { class: "muted", text: "Not a registered agent. This name wrote or rated memories without an agent record (the admin key, or a key from before agent registration)." }));
  }
  const pairs = [
    ["Status", h("span", {}, statusBadge(agent.status),
      agent.key_locked && agent.status === "active"
        ? [" ", h("span", { class: "badge badge-revoked", title: "Its key was locked from the agent portal; replace the key", text: "key locked" })]
        : null)],
    ["Trust level", agent.status === "pending" ? h("span", { class: "muted", text: "—" }) : levelBadge(agent.trust_level)],
    ["Home fleet", agent.home_fleet_id ? link(fleetName(agent.home_fleet_id), memoriesHref({ fleet: agent.home_fleet_id })) : "—"],
    ["Owner", agent.owner_alias || "—"],
    ["Registered", h("span", { title: fmtTime(agent.created_at), text: agent.created_at ? fmtAgo(agent.created_at) : "—" })],
    ["Activated", h("span", { title: fmtTime(agent.activated_at), text: agent.activated_at ? fmtAgo(agent.activated_at) : "—" })],
  ];
  return card("Registration", h("dl", { class: "meta" }, pairs.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)])),
    { head: [h("div", { class: "row tight" }, agentButtons(agent, () => hooks.refresh()))] });
}

function numbers(row, flags, days) {
  const received = row ? row.received : { helpful: 0, stale: 0, wrong: 0 };
  return h("div", { class: "stats" },
    stat("Memories written", row ? fmtNum(row.entries) : "—", { hint: row ? `${fmtNum(row.active)} still active` : null }),
    stat(`Writes, ${days}d`, row ? fmtNum(row.recent_writes) : "—", { hint: row && row.last_write_at ? `last ${fmtAgo(row.last_write_at)}` : null }),
    stat("Verdicts given", row ? fmtNum(row.verdicts_given) : "—", { hint: row ? `${fmtNum(row.recent_verdicts)} in ${days}d` : null }),
    stat("Verdicts received", row ? fmtNum(received.helpful + received.stale + received.wrong) : "—",
      { hint: row ? `${received.helpful} helpful · ${received.stale} stale · ${received.wrong} wrong` : null }),
    stat("Flags confirmed", flags ? `${flags.confirmed} of ${flags.flags}` : "—", { hint: "Its stale or wrong reports later confirmed by a supersession" }));
}

export async function viewAgent(root) {
  const name = parseHash().params.get("name") || "";
  if (!name) {
    fill(root, pageHead("Agent", null, link("All agents", "#/agents", { class: "link small" })),
      card(null, h("p", { class: "muted", text: "No agent named. Open one from the Agents or Stats page." })));
    return;
  }
  const [, metrics, activity] = await Promise.all([loadAgentsAndFleets(), fetchMetrics(), fetchActivity(state.days)]);
  const agent = state.agents.find((a) => a.name === name) || null;
  const row = activity ? (activity.agents || []).find((a) => a.name === name) || null : null;
  const flags = (metrics && metrics.feedback && metrics.feedback.by_agent && metrics.feedback.by_agent[name]) || null;

  const memories = entryTable({
    fetchPage: ({ limit, offset }) => guarded(() => api("GET", "admin/entries", { query: { author: name, include_inactive: "true", limit, offset } })),
    onOpen: (id) => openEntry(id, () => memories.load(true)),
    fleetName,
    emptyText: "It has not written any memories.",
    csvName: `agent-${name}-memories`,
    statusText: (n) => `${n} shown, newest first, withdrawn and superseded included`,
  });
  const given = feedbackTable({
    fetchPage: ({ limit, offset }) => guarded(() => api("GET", "admin/feedback", { query: { reporter: name, limit, offset } })),
    onOpen: (id) => openEntry(id, () => given.load(true)),
    fleetName,
    agentHref,
    emptyText: "It has not rated any memory.",
    csvName: `agent-${name}-verdicts-given`,
  });
  const received = feedbackTable({
    fetchPage: ({ limit, offset }) => guarded(() => api("GET", "admin/feedback", { query: { author: name, limit, offset } })),
    onOpen: (id) => openEntry(id, () => received.load(true)),
    fleetName,
    agentHref,
    emptyText: "Nobody has rated its memories yet.",
    csvName: `agent-${name}-verdicts-received`,
  });

  const section = (title, table, href) => h("section", { class: "agent-sec" },
    h("div", { class: "section-head" }, h("h2", { text: title }), link("Open with filters", href, { class: "link small" })),
    table.el);

  fill(root, 
    pageHead(name, agent && agent.owner_alias ? `Owned by ${agent.owner_alias}` : null,
      link("All agents", "#/agents", { class: "link small" }), windowSelector(() => hooks.refresh()), refreshButton()),
    numbers(row, flags, state.days),
    profile(agent),
    section("Memories it wrote", memories, memoriesHref({ author: name, all: "1" })),
    section("Verdicts it gave", given, feedbackHref({ reporter: name })),
    section("Verdicts on its memories", received, feedbackHref({ author: name })));
  await Promise.all([memories.load(true), given.load(true), received.load(true)]);
}

