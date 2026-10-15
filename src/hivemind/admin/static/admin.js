// Hivemind admin panel (ADR 0029): entry point, routing and session.
//
// The panel is plain ES modules served from this directory (no build step):
//   core.js      helpers, API client, formatting, modal, shared widgets
//   charts.js    the SVG activity chart
//   drawer.js    the memory detail drawer
//   overview.js  memories.js  stats.js  manage.js  health.js   the views
//
// Rules every module keeps: API data reaches the page through textContent
// (h(), svg()) and never through an HTML-parsing sink; the admin key lives in
// sessionStorage for this tab only; a new key is shown once.

import {
  $, h, api, hooks, hasKey, setKey, clearKey, closeModal, guarded, parseHash,
} from "./core.js";
import { closeDrawer, initDrawer } from "./drawer.js";
import { viewOverview } from "./overview.js";
import { viewMemories } from "./memories.js";
import { viewStats } from "./stats.js";
import { viewAgents, viewFleets, viewAudit, viewKeys } from "./manage.js";
import { viewHealth } from "./health.js";

const VIEWS = {
  overview: viewOverview, memories: viewMemories, stats: viewStats, agents: viewAgents,
  fleets: viewFleets, health: viewHealth, audit: viewAudit, keys: viewKeys,
};

let renderToken = 0;
let currentContainer = null;

async function route() {
  if (!hasKey()) { showLogin(); return; }
  const { view: requested } = parseHash();
  let name = "overview";
  let view = viewOverview;
  switch (requested) {
    case "overview": name = "overview"; view = viewOverview; break;
    case "memories": name = "memories"; view = viewMemories; break;
    case "stats": name = "stats"; view = viewStats; break;
    case "agents": name = "agents"; view = viewAgents; break;
    case "fleets": name = "fleets"; view = viewFleets; break;
    case "health": name = "health"; view = viewHealth; break;
    case "audit": name = "audit"; view = viewAudit; break;
    case "keys": name = "keys"; view = viewKeys; break;
    default: break;
  }
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
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
