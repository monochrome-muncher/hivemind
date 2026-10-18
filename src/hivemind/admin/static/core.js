// Hivemind admin panel (ADR 0029): the admin session and shared state.
//
// The generic widgets live in ui.js and the API client in client.js; this
// module re-exports both, so the views import everything from here. What is
// admin-only stays here: the admin key's storage slot, the sign-out on 401,
// the agent/fleet cache and the admin routes the views share.
//
// The admin key lives in sessionStorage for this tab only. It is sent as
// X-API-Key to the same-origin proxy and nowhere else.

import { $, h } from "./ui.js";
import { ApiError, createClient } from "./client.js";

export * from "./ui.js";
export { ApiError } from "./client.js";

export const KEY_STORAGE = "hivemind.adminKey";
export const WINDOWS = [7, 30, 90];

const client = createClient({ storageKey: KEY_STORAGE, appName: "The admin panel" });
export const { getKey, setKey, clearKey, hasKey, apiRaw, api } = client;

// --- navigation hooks (set by admin.js, so modules need no import cycle) --------------

export const hooks = {
  refresh: () => {},
  rerenderStats: () => {},
  signOut: () => {},
};

/** Run an API call; on 401 sign out, otherwise surface the error. */
export async function guarded(fn) {
  try {
    return await fn();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      hooks.signOut("Your admin key is no longer valid. Sign in again.");
      return undefined;
    }
    throw err;
  }
}

/**
 * Run an optional call (a card that may be missing on an older API): any
 * failure but a 401 resolves to null so the card can say "not available".
 */
export async function optional(fn) {
  try {
    return await fn();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) throw err;
    return null;
  }
}

// --- data cache (refreshed per view) ---------------------------------------------------------

export const state = {
  agents: [],
  fleets: [],
  metrics: null,
  days: 30, // the activity window shared by Overview and Stats
};

export async function loadAgentsAndFleets() {
  const [agents, fleets] = await Promise.all([api("GET", "admin/agents"), api("GET", "admin/fleets")]);
  state.agents = agents;
  state.fleets = fleets;
  updatePendingPill();
}

export function fleetName(id) {
  if (!id) return "—";
  const fleet = state.fleets.find((f) => f.id === id);
  return fleet ? fleet.name : id;
}

export function updatePendingPill() {
  const count = state.agents.filter((a) => a.status === "pending").length;
  const pill = $("nav-pending");
  pill.textContent = String(count);
  pill.hidden = count === 0;
}

export const fetchMetrics = () => optional(() => api("GET", "metrics"));
export const fetchActivity = (days) => optional(() => api("GET", "admin/activity", { query: { days } }));

// --- admin layout pieces ----------------------------------------------------------------

export const refreshButton = () => h("button", { type: "button", class: "btn btn-sm btn-ghost", text: "Refresh", onclick: () => hooks.refresh() });

/** A segmented 7/30/90 selector bound to state.days. */
export function windowSelector(onChange) {
  const group = h("div", { class: "segmented", role: "group", "aria-label": "Time window" });
  const paint = () => group.replaceChildren(...WINDOWS.map((d) => h("button", {
    type: "button", class: d === state.days ? "seg active" : "seg", "aria-pressed": d === state.days ? "true" : "false",
    text: `${d} days`,
    onclick: () => { state.days = d; paint(); onChange(d); },
  })));
  paint();
  return group;
}
