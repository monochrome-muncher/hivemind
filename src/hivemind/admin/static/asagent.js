// Hivemind admin panel: "Search as an agent" (ADR 0136). Pure: no DOM.

import { levelName } from "./ui.js";

const sameName = (id) => id;

/** The banner over the agent's results. */
export function asAgentBanner(agent, fleetName = sameName) {
  const home = agent.home_fleet_id ? fleetName(agent.home_fleet_id) : "none";
  return `Results as ${agent.name} sees them (${levelName(agent.trust_level)}, home fleet ${home})`;
}

/** The authors whose memories `name` owns: itself and, transitively, every agent handing down to it (ADR 0113). */
export function ownedAuthors(name, successions) {
  const owned = new Set([name]);
  for (let grew = true; grew;) {
    grew = false;
    for (const s of successions) {
      if (owned.has(s.successor) && !owned.has(s.retired_agent)) { owned.add(s.retired_agent); grew = true; }
    }
  }
  return owned;
}

/**
 * Why `agent` cannot see `hit` (a normalised entry), from the agent's record and the hit alone.
 * `owned` is the agent's ownedAuthors; `includeInactive` is the Include inactive filter.
 */
export function hiddenReason(agent, hit, { fleetName = sameName, owned = new Set([agent.name]), includeInactive = false } = {}) {
  const own = owned.has(hit.author);
  if (agent.trust_level === 0) return "untrusted agents read no memories";
  if (hit.scope === "self" && !own) return `a private note of ${hit.author}`;
  if (hit.scope === "fleet" && !own && agent.trust_level < 3 && hit.fleet_id !== agent.home_fleet_id) {
    return `in fleet ${fleetName(hit.fleet_id)}, not its home fleet`;
  }
  if (hit.state !== "active" && !includeInactive) return `${hit.state}, so out of an ordinary search`;
  return "ranked below the cut-off or filtered";
}

/** The admin's hits the agent's own search did not return, each with its reason (`options` as for hiddenReason). */
export function hiddenFrom(agent, adminHits, agentHits, options = {}) {
  const seen = new Set(agentHits.map((hit) => hit.id));
  return adminHits.filter((hit) => !seen.has(hit.id)).map((hit) => ({ hit, reason: hiddenReason(agent, hit, options) }));
}
