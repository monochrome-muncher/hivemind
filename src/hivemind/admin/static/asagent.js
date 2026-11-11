// Hivemind admin panel: "Search as an agent" (ADR 0136). Pure: no DOM.

import { levelName } from "./ui.js";

const sameName = (id) => id;

/** The banner over the agent's results. */
export function asAgentBanner(agent, fleetName = sameName) {
  const home = agent.home_fleet_id ? fleetName(agent.home_fleet_id) : "none";
  return `Results as ${agent.name} sees them (${levelName(agent.trust_level)}, home fleet ${home})`;
}

/** Why `agent` cannot see `hit` (a normalised entry), from the agent's record and the hit alone. */
export function hiddenReason(agent, hit, fleetName = sameName) {
  const own = hit.author === agent.name;
  if (agent.trust_level === 0) return "untrusted agents read no memories";
  if (hit.scope === "self" && !own) return `a private note of ${hit.author}`;
  if (hit.scope === "fleet" && !own && agent.trust_level < 3 && hit.fleet_id !== agent.home_fleet_id) {
    return `in fleet ${fleetName(hit.fleet_id)}, not its home fleet`;
  }
  if (hit.state !== "active") return `${hit.state}, so out of an ordinary search`;
  return "ranked below the cut-off or filtered";
}

/** The admin's hits the agent's own search did not return, each with its reason. */
export function hiddenFrom(agent, adminHits, agentHits, fleetName = sameName) {
  const seen = new Set(agentHits.map((hit) => hit.id));
  return adminHits.filter((hit) => !seen.has(hit.id)).map((hit) => ({ hit, reason: hiddenReason(agent, hit, fleetName) }));
}
