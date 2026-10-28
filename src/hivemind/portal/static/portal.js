// Hivemind agent portal (ADR 0095): entry point, routing and session.
//
// Plain ES modules, no build step. The portal's own files:
//   portal.js   this file: sign-in, routing, the drawer's configuration
//   session.js  the agent-key session and who the key belongs to
//   views.js    the pages
// and the admin panel's shared modules, served as they are (ADR 0091):
//   ui.js client.js lists.js csv.js drawer.js charts.js
//
// Rules every module keeps: API data reaches the page through textContent
// (h()) and never through an HTML-parsing sink; the agent key lives in
// sessionStorage for this tab only, in its own slot; one key at a time.

import { $, h, api, hooks, hasKey, setKey, clearKey, closeModal, openModal, guarded, refusedMessage, parseHash, state, refusal, isMine, fleetName, levelName, viewHref } from "./session.js";
import { closeDrawer, configureDrawer, initDrawer } from "./drawer.js";
import { viewMemories, viewGiven, viewReceived, viewFleet, viewSearch } from "./views.js";

const VIEWS = { memories: viewMemories, given: viewGiven, received: viewReceived, fleet: viewFleet, search: viewSearch };

let renderToken = 0;
let currentContainer = null;

async function route() {
  if (!hasKey()) { showLogin(); return; }
  const mine = ++renderToken;
  if (!state.me) {
    const ok = await loadMe(mine);
    if (!ok || mine !== renderToken) return;
  }
  const { view: requested } = parseHash();
  const name = Object.prototype.hasOwnProperty.call(VIEWS, requested) ? requested : "memories";
  const view = VIEWS[name];
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  const page = $("view");
  if (!page.firstChild) page.append(h("p", { class: "loading", text: "Loading…" }));
  // Each navigation renders into its own container, which replaces the
  // page only if no newer navigation started meanwhile (as in the admin panel).
  const container = h("div", { class: "view-inner" });
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

/**
 * Who the stored key belongs to; signs out if it is not (or no longer) an
 * agent key. `mine` is the navigation asking: an answer that lands after a
 * sign-out or another sign-in is dropped, so it never names the wrong agent.
 */
async function loadMe(mine) {
  let standing;
  try {
    standing = await api("GET", "whoami");
  } catch (err) {
    if (mine !== renderToken) return false;
    if (err.status === 401) { signOut(refusedMessage(err)); return false; }
    $("view").replaceChildren(h("p", { class: "form-error", text: `Could not check the key: ${err.message}` }));
    return false;
  }
  if (mine !== renderToken) return false; // a sign-out or another sign-in came first
  const why = refusal(standing);
  if (why) { signOut(why); return false; }
  state.me = standing;
  paintWho();
  return true;
}

function paintWho() {
  const me = state.me;
  $("who").replaceChildren(
    h("span", { class: "who-name", text: me.name }),
    h("span", { class: "muted small", text: `${levelName(me.trust_level)} · ${me.home_fleet_id ? fleetName(me.home_fleet_id) : "no home fleet"}` }));
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
  state.me = null;
  closeModal();
  closeDrawer();
  renderToken += 1; // drop any view still loading
  $("view").replaceChildren();
  $("who").replaceChildren();
  showLogin(message);
}

async function signIn(event) {
  event.preventDefault();
  const input = $("login-key");
  const key = input.value.trim();
  if (!key) return;
  const button = event.target.querySelector("button");
  button.disabled = true;
  renderToken += 1; // a whoami still in flight for an earlier key must not land
  input.value = "";
  try {
    const standing = await api("GET", "whoami", { key });
    const why = refusal(standing);
    if (why) { showLogin(why); return; }
    setKey(key);
    state.me = standing;
    paintWho();
    showShell();
    checkApi();
    await route();
  } catch (err) {
    showLogin(err.status === 401
      ? refusedMessage(err, "That key is not recognised. A pending or revoked agent's key cannot sign in.")
      : err.message);
  } finally {
    button.disabled = false;
  }
}

/** "This key leaked": confirm, lock the signed-in key, sign out and say what to do next. */
function leakedDialog() {
  const name = state.me ? state.me.name : "this agent";
  openModal({
    title: "Lock this key?",
    body: [
      h("p", { text: `Use this if the key for ${name} has leaked or may be in the wrong hands. The key stops working on its next request, everywhere: the API, the MCP server and this portal. You will be signed out now, and this cannot be undone from the portal.` }),
      h("p", { text: `${name} itself is not touched: its memories, pins, ratings, trust level and fleet stay as they are. To get back in, ask the admin to replace the key. The admin issues a new one and the locked key stays dead.` }),
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Lock this key",
        class: "btn-danger-solid",
        onClick: async () => {
          try {
            await api("POST", "portal/key/lock");
          } catch (err) {
            // Another tab locked it, or the admin replaced it first: this key is no longer usable either way.
            if (err.code === "key_locked" || err.code === "key_not_locked") { signOut(err.message); return; }
            throw err;
          }
          signOut(`The key for ${name} is locked and no longer works anywhere. Ask the admin for a new key for ${name}. Its memories, pins, ratings, trust level and fleet are unchanged.`);
        },
      },
    ],
  });
}

async function checkApi() {
  try {
    await api("GET", "health");
    $("api-status").textContent = "API connected";
  } catch {
    $("api-status").textContent = "API unreachable";
  }
}

// The drawer as an agent sees it: what its key can read, its own memories withdrawable with a reason.
configureDrawer({
  fetchEntry: (id) => guarded(() => api("GET", `entries/${encodeURIComponent(id)}`, { query: { history: "true" } })),
  withdrawEntry: (id, reason) => guarded(() => api("POST", `portal/entries/${encodeURIComponent(id)}/withdraw`, { body: { reason } })),
  canWithdraw: isMine,
  reasonRequired: true,
  fleetName,
  fleetHref: (fleet) => (state.me && fleet === state.me.home_fleet_id ? viewHref("fleet") : null),
  feedbackHref: (entry, item) => (isMine(item) ? viewHref("received", { entry }) : null),
});

hooks.refresh = () => { state.me = null; return route(); };
hooks.signOut = signOut;

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", signIn);
  $("logout").addEventListener("click", () => signOut());
  $("leaked").addEventListener("click", leakedDialog);
  $("modal").addEventListener("close", () => $("modal-body").replaceChildren());
  initDrawer();
  window.addEventListener("hashchange", () => { closeDrawer(); route(); });
  if (hasKey()) { showShell(); route(); checkApi(); } else { showLogin(); }
});
