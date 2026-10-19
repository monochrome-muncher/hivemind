// Hivemind agent portal (ADR 0095): the agent-key session and shared state.
//
// The generic widgets live in ui.js and the API client in client.js (the
// admin panel's shared modules, ADR 0091); this module re-exports both, so
// the views import everything from here. What is portal-only stays here:
// the agent key's own storage slot (never the admin panel's), the sign-out
// on 401, and who the key belongs to.

import { h } from "./ui.js";
import { ApiError, createClient } from "./client.js";

export * from "./ui.js";
export { ApiError } from "./client.js";

export const KEY_STORAGE = "hivemind.portalKey";

const client = createClient({ storageKey: KEY_STORAGE, appName: "The agent portal" });
export const { getKey, setKey, clearKey, hasKey, apiRaw, api } = client;

export const hooks = {
  refresh: () => {},
  signOut: () => {},
};

/** Run an API call; on 401 sign out (unless the key changed meanwhile), otherwise surface the error. */
export async function guarded(fn) {
  const key = getKey();
  try {
    return await fn();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      if (getKey() !== key) return undefined; // an earlier key's answer: not this session's
      hooks.signOut("Your agent key is no longer valid (the agent may have been revoked). Sign in again.");
      return undefined;
    }
    throw err;
  }
}

// The key's standing from GET /v1/whoami, refreshed on every sign-in and page load.
export const state = { me: null };

/** Whether `standing` (a whoami answer) may use the portal, and if not, why. */
export function refusal(standing) {
  if (standing.key_kind === "agent") return null;
  if (standing.key_kind === "admin") return "That is an admin key. Use the admin panel with it; the portal takes an agent key.";
  if (standing.key_kind === "org") return "That is the org key, which only registers agents. Sign in with one agent's key.";
  return "That key does not belong to a registered agent.";
}

export const isMine = (entry) => Boolean(state.me && entry && entry.author === state.me.name);

/** A fleet's name as this agent can know it: its home fleet by name, any other by a short id. */
export function fleetName(id) {
  if (!id) return "—";
  const me = state.me;
  if (me && id === me.home_fleet_id) return me.home_fleet_name || "home fleet";
  return `fleet ${id.slice(0, 8)}`;
}

export const refreshButton = () => h("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Refresh", onclick: () => hooks.refresh() });
