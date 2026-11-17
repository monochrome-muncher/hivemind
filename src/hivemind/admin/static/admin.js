// Hivemind admin panel (ADR 0029): entry point, routing and session.
//
// The panel is plain ES modules served from this directory (no build step).
//
// Shared modules (import nothing admin-specific, ADR 0091), so the portal
// serves them as they are:
//   ui.js         generic DOM helpers, formatting, badges, modal, layout pieces
//   client.js     the API client (one key per tab, its own storage slot)
//   lists.js      paged memory, feedback and flagged tables
//   memfilters.js the memory filters (state, tags, entity, dates)
//   csv.js        CSV export of a list
//   drawer.js     the memory detail drawer (configured below)
//   charts.js     the SVG activity chart
//   explain.js    "Explain ranking": a hit's score as plain lines
//   since.js      "since you last looked" (last visit in browser storage)
//
// The admin session and core:
//   admin.js      this file: entry point, routing, login
//   core.js       the admin session and shared state; re-exports ui.js and client.js
//
// The views (pages):
//   overview.js   the landing view: Needs you, pending queue, KPIs
//   memories.js   search and the newest-first listing
//   feedback.js   the Feedback log (who rated what)
//   stats.js      Fleets, Agents and Users rolled up from activity
//   agents.js     the Agents list, with its agent dialogs and Scout card
//   fleets.js     the Fleets list, with rename, phase and pins
//   audit.js      the audit log and its rows
//   keys.js       rotating the org key
//   health.js     fleet health (observation only)
//   agent.js      one agent's page
//   owner.js      one owner's page
//   fleet.js      one fleet's page
//
// The dialogs and cards the views use:
//   cleanup.js    cleaning up after a bad agent, and undoing it
//   merge.js      merging fleets, moving agents, and undoing either
//   retire.js     retiring an agent with a successor, and undoing it
//   offboard.js   offboarding an owner
//   menu.js       the "More" dropdown on an agent row
//
// Pure helpers (no DOM, so a test runs them as they ship):
//   asagent.js    "Search as an agent"
//   needs.js      what waits on the operator
//   vitals.js     the vital signs of the hive
//   fleetdata.js  what the fleet page works out of its lists
//
// Rules every module keeps: API data reaches the page through textContent
// (h(), svg()) and never through an HTML-parsing sink; the admin key lives in
// sessionStorage for this tab only; a new key is shown once.

import {
  $, h, api, hooks, hasKey, setKey, clearKey, closeModal, guarded, parseHash, fleetName, memoriesHref, feedbackHref, agentHref,
} from "./core.js";
import { closeDrawer, configureDrawer, initDrawer } from "./drawer.js";
import { viewOverview } from "./overview.js";
import { viewMemories } from "./memories.js";
import { viewFeedback } from "./feedback.js";
import { viewStats } from "./stats.js";
import { viewAgents } from "./agents.js";
import { viewFleets } from "./fleets.js";
import { viewAudit } from "./audit.js";
import { viewKeys } from "./keys.js";
import { viewHealth } from "./health.js";
import { viewAgent } from "./agent.js";
import { viewOwner } from "./owner.js";
import { viewFleet } from "./fleet.js";

const VIEWS = {
  overview: viewOverview, memories: viewMemories, feedback: viewFeedback, stats: viewStats, agents: viewAgents,
  fleets: viewFleets, health: viewHealth, audit: viewAudit, keys: viewKeys, agent: viewAgent, owner: viewOwner, fleet: viewFleet,
};
// A detail page lights up the list it belongs to in the sidebar.
const NAV_PARENT = { agent: "agents", owner: "agents", fleet: "fleets" };

let renderToken = 0;
let currentContainer = null;

async function route() {
  if (!hasKey()) { showLogin(); return; }
  const { view: requested } = parseHash();
  const isKnownView = Object.prototype.hasOwnProperty.call(VIEWS, requested);
  const name = isKnownView ? requested : "overview";
  const selectedView = VIEWS[name];
  const view = typeof selectedView === "function" ? selectedView : viewOverview;
  const navName = NAV_PARENT[name] || name;
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === navName));
  const page = $("view");
  const mine = ++renderToken;
  if (!page.firstChild) page.append(h("p", { class: "loading", text: "Loading…" }));
  // Each navigation renders into its own container, which replaces the page
  // only if no newer navigation started meanwhile. The container stays the
  // view's root for later re-renders (tabs, filters); once superseded it is
  // detached, so a late render into it is invisible.
  const container = h("div", { class: "view-inner" });
  // Tell the previous view it is superseded, so a late callback of it (the
  // Memories view syncing the hash) leaves the new navigation alone.
  if (currentContainer) currentContainer.dataset.superseded = "1";
  currentContainer = container;
  try {
    await guarded(() => view(container));
    if (mine === renderToken) page.replaceChildren(container);
  } catch (err) {
    if (mine !== renderToken) return;
    page.replaceChildren(h("div", { class: "card" }, h("div", { class: "card-body" },
      h("p", { class: "form-error", text: `Could not load this page: ${err.message}` }))));
  }
}

function showLogin(message) {
  $("shell").hidden = true;
  $("login").hidden = false;
  const errorEl = $("login-error");
  errorEl.textContent = message || "";
  errorEl.hidden = !message;
  $("login-key").focus();
}

function showShell() {
  $("login").hidden = true;
  $("shell").hidden = false;
}

function signOut(message) {
  clearKey();
  closeModal();
  closeDrawer();
  renderToken += 1; // drop any view still loading
  $("view").replaceChildren();
  showLogin(message);
}

async function signIn(event) {
  event.preventDefault();
  const input = $("login-key");
  const key = input.value.trim();
  if (!key) return;
  const button = event.target.querySelector("button");
  button.disabled = true;
  try {
    // Any admin-gated read proves the key: 401 unknown, 403 not an admin key.
    await api("GET", "admin/fleets", { key });
    setKey(key);
    input.value = "";
    showShell();
    checkApi();
    await route();
  } catch (err) {
    const message = err.status === 401 ? "That key is not recognised."
      : err.status === 403 ? "That is not an admin key (an org or agent key cannot sign in here)."
      : err.message;
    showLogin(message);
  } finally {
    button.disabled = false;
  }
}

async function checkApi() {
  try {
    await api("GET", "health");
    $("api-status").textContent = "API connected";
  } catch {
    $("api-status").textContent = "API unreachable";
  }
}

// The drawer as the admin sees it: any entry, withdrawable, linked into the panel's views.
configureDrawer({
  fetchEntry: (id) => guarded(() => api("GET", `admin/entries/${encodeURIComponent(id)}`)),
  withdrawEntry: (id, reason) => guarded(() => api("POST", `admin/entries/${encodeURIComponent(id)}/withdraw`, { body: reason ? { reason } : {} })),
  redactEntry: (id, reason, confirm) => guarded(() => api("POST", `admin/entries/${encodeURIComponent(id)}/redact`, { body: { reason, confirm } })),
  restoreEntry: (id, reason) => guarded(() => api("POST", `admin/entries/${encodeURIComponent(id)}/restore`, { body: { reason } })),
  moveToSelf: (id, reason) => guarded(() => api("POST", `admin/entries/${encodeURIComponent(id)}/move-to-self`, { body: { reason } })),
  fleetName,
  authorHref: (author) => agentHref(author),
  fleetHref: (fleet) => memoriesHref({ fleet }),
  feedbackHref: (entry) => feedbackHref({ entry }),
});

hooks.refresh = route;
hooks.signOut = signOut;

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", signIn);
  $("logout").addEventListener("click", () => signOut());
  $("modal").addEventListener("cancel", (event) => {
    if ($("modal").dataset.dismissable === "no") event.preventDefault();
  });
  $("modal").addEventListener("close", () => $("modal-body").replaceChildren());
  initDrawer();
  window.addEventListener("hashchange", () => { closeDrawer(); route(); });
  if (hasKey()) { showShell(); route(); checkApi(); } else { showLogin(); }
});
