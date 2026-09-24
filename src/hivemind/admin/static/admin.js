// Hivemind admin panel (ADR 0029).
//
// Rules this file keeps:
// * Every piece of API data reaches the page through textContent (the
//   h() helper) and never through an HTML-parsing sink, so an agent name
//   or owner alias cannot inject markup. The CSP forbids inline script as a second line.
// * The admin key lives in sessionStorage for this tab only. It is sent
//   as X-API-Key to the same-origin proxy and nowhere else.
// * A freshly issued key is shown once, in the modal, and never stored.

const KEY_STORAGE = "hivemind.adminKey";
const LEVELS = [
  { value: 0, name: "untrusted", hint: "no access (dormant)" },
  { value: 1, name: "lurker", hint: "own + home-fleet read, own write" },
  { value: 2, name: "contributor", hint: "+ home-fleet write" },
  { value: 3, name: "privileged", hint: "+ read across all fleets" },
];
const AUDIT_ACTIONS = [
  "agent.activate", "agent.trust_level_set", "agent.home_fleet_set", "agent.revoke",
  "fleet.create", "org_key.rotate", "entry.withdraw",
  "admin_key.issue", "admin_key.revoke", "agent_key.issue",
];
const AUDIT_PAGE = 50;

// --- tiny DOM helper ---------------------------------------------------------

/** Build an element. Strings and numbers become text nodes (never HTML). */
function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const $ = (id) => document.getElementById(id);

// --- session + API -------------------------------------------------------------

function getKey() {
  try { return sessionStorage.getItem(KEY_STORAGE); } catch { return null; }
}
function setKey(key) {
  try { sessionStorage.setItem(KEY_STORAGE, key); } catch { /* private mode */ }
  memoryKey = key;
}
function clearKey() {
  try { sessionStorage.removeItem(KEY_STORAGE); } catch { /* ignore */ }
  memoryKey = null;
}
let memoryKey = null; // fallback when sessionStorage is unavailable

class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function api(method, path, { body, query, key } = {}) {
  const url = new URL(`/v1/${path}`, window.location.origin);
  for (const [k, v] of Object.entries(query || {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v);
  }
  const headers = { "X-API-Key": key || getKey() || memoryKey || "", Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
      credentials: "omit",
    });
  } catch {
    throw new ApiError(0, "network", "The admin panel could not be reached.");
  }
  let payload = null;
  const text = await response.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = null; }
  }
  if (!response.ok) {
    const err = (payload && payload.error) || {};
    let message = err.message || (payload && typeof payload.detail === "string" ? payload.detail : "");
    if (!message && payload && Array.isArray(payload.detail)) {
      message = payload.detail.map((d) => d.msg).join("; ");
    }
    throw new ApiError(response.status, err.code || "http_" + response.status, message || response.statusText);
  }
  return payload;
}

/** Run an API call; on 401 sign out, otherwise surface the error. */
async function guarded(fn) {
  try {
    return await fn();
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      signOut("Your admin key is no longer valid. Sign in again.");
      return undefined;
    }
    throw err;
  }
}

// --- formatting -----------------------------------------------------------------

const levelName = (value) => (LEVELS.find((l) => l.value === value) || { name: String(value) }).name;

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function fmtAgo(iso) {
  if (!iso) return "—";
  const seconds = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  const units = [["day", 86400], ["hour", 3600], ["minute", 60]];
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) {
      return new Intl.RelativeTimeFormat(undefined, { numeric: "auto" }).format(-Math.round(seconds / size), unit);
    }
  }
  return "just now";
}

const statusBadge = (status) => h("span", { class: `badge badge-${status}`, text: status });
const levelBadge = (value) => h("span", { class: "badge badge-level", text: `${value} · ${levelName(value)}` });

function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = isError ? "toast toast-error" : "toast";
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 6000 : 3500);
}

// --- modal ----------------------------------------------------------------------

/**
 * Open the shared modal. `actions` are {label, class, onClick} where
 * onClick may return a promise; a thrown error is shown in the modal and
 * keeps it open. Returns nothing: callers act inside onClick.
 */
function openModal({ title, body, actions, dismissable = true }) {
  const modal = $("modal");
  $("modal-title").textContent = title;
  $("modal-body").replaceChildren(...[].concat(body));
  const errorEl = $("modal-error");
  errorEl.hidden = true;
  const buttons = actions.map((action) => {
    const btn = h("button", { type: action.submit ? "submit" : "button", class: `btn ${action.class || ""}`, text: action.label });
    btn.addEventListener("click", async (event) => {
      event.preventDefault();
      if (!action.onClick) { closeModal(); return; }
      buttons.forEach((b) => { b.disabled = true; });
      errorEl.hidden = true;
      try {
        await action.onClick();
      } catch (err) {
        errorEl.textContent = err.message || String(err);
        errorEl.hidden = false;
      } finally {
        buttons.forEach((b) => { b.disabled = false; });
      }
    });
    return btn;
  });
  $("modal-actions").replaceChildren(...buttons);
  modal.dataset.dismissable = dismissable ? "yes" : "no";
  if (!modal.open) modal.showModal();
  const first = modal.querySelector("input, select");
  if (first) first.focus();
}

function closeModal() {
  const modal = $("modal");
  $("modal-body").replaceChildren(); // drop any key from the DOM
  if (modal.open) modal.close();
}

/** The one place a raw key is ever shown. It is not stored anywhere. */
function showKeyOnce(title, key, note) {
  const box = h("div", { class: "key-box", text: key });
  const copy = h("button", { type: "button", class: "btn btn-sm", text: "Copy" });
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(key);
      copy.textContent = "Copied";
    } catch {
      const range = document.createRange();
      range.selectNodeContents(box);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      copy.textContent = "Selected — press Ctrl+C";
    }
  });
  openModal({
    title,
    dismissable: false,
    body: [
      h("p", { class: "callout callout-warn", text: "This key is shown once. Store it now — it cannot be retrieved later." }),
      box,
      h("div", { class: "row" }, copy),
      note ? h("p", { class: "muted small", text: note }) : null,
    ],
    actions: [{ label: "I have stored the key", class: "btn-primary", onClick: async () => closeModal() }],
  });
}

// --- data cache (refreshed per view) ---------------------------------------------

const state = { agents: [], fleets: [], metrics: null };

async function loadAgentsAndFleets() {
  const [agents, fleets] = await Promise.all([api("GET", "admin/agents"), api("GET", "admin/fleets")]);
  state.agents = agents;
  state.fleets = fleets;
  updatePendingPill();
}

function fleetName(id) {
  if (!id) return "—";
  const fleet = state.fleets.find((f) => f.id === id);
  return fleet ? fleet.name : id;
}

function updatePendingPill() {
  const count = state.agents.filter((a) => a.status === "pending").length;
  const pill = $("nav-pending");
  pill.textContent = String(count);
  pill.hidden = count === 0;
}

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

function agentActions(agent, refresh) {
  const buttons = [];
  if (agent.status === "pending") {
    buttons.push(h("button", { type: "button", class: "btn btn-primary btn-sm", text: "Activate", onclick: () => activateDialog(agent, refresh) }));
    buttons.push(h("button", { type: "button", class: "btn btn-danger btn-sm", text: "Reject", onclick: () => revokeDialog(agent, refresh) }));
  } else if (agent.status === "active") {
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Edit", onclick: () => editDialog(agent, refresh) }));
    buttons.push(h("button", { type: "button", class: "btn btn-danger btn-sm", text: "Revoke", onclick: () => revokeDialog(agent, refresh) }));
  } else if (agent.status === "revoked") {
    buttons.push(h("button", { type: "button", class: "btn btn-sm", text: "Re-activate", onclick: () => activateDialog(agent, refresh) }));
  }
  return h("td", { class: "actions" }, buttons);
}

function table(headers, rows, emptyText) {
  return h("div", { class: "table-wrap" },
    h("table", {},
      h("thead", {}, h("tr", {}, headers.map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { class: "empty", colspan: headers.length, text: emptyText })))));
}

// --- views ------------------------------------------------------------------------

function pageHead(title, subtitle, ...extra) {
  return h("div", { class: "page-head" },
    h("div", {}, h("h1", { text: title }), subtitle ? h("p", { class: "muted", text: subtitle }) : null),
    ...extra);
}

function stat(label, value, warn = false) {
  return h("div", { class: warn ? "stat stat-warn" : "stat" },
    h("div", { class: "stat-label", text: label }), h("div", { class: "stat-value", text: value }));
}

function bars(counts) {
  const entries = Object.entries(counts);
  const max = Math.max(1, ...entries.map(([, v]) => v));
  return h("div", { class: "bars" }, entries.length === 0 ? h("p", { class: "muted", text: "Nothing yet." }) :
    entries.map(([label, value]) => {
      const fill = h("div", { class: "bar-fill" });
      fill.style.width = `${Math.round((value / max) * 100)}%`;
      return h("div", { class: "bar-row" }, h("span", { text: label }), h("div", { class: "bar-track" }, fill),
        h("span", { class: "bar-count", text: value }));
    }));
}

function pendingQueue(refresh) {
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

async function viewOverview(root) {
  const refresh = () => route();
  const [, metrics] = await Promise.all([loadAgentsAndFleets(), api("GET", "metrics").catch(() => null)]);
  state.metrics = metrics;
  const agentCounts = {
    pending: state.agents.filter((a) => a.status === "pending").length,
    active: state.agents.filter((a) => a.status === "active").length,
    revoked: state.agents.filter((a) => a.status === "revoked").length,
  };
  const activeByLevel = {};
  for (const l of LEVELS) activeByLevel[l.name] = state.agents.filter((a) => a.status === "active" && a.trust_level === l.value).length;
  const agentsPerFleet = {};
  for (const f of state.fleets) agentsPerFleet[f.name] = state.agents.filter((a) => a.status === "active" && a.home_fleet_id === f.id).length;

  root.replaceChildren(
    pageHead("Overview", "Registrations waiting on you, and the shape of the cluster."),
    h("div", { class: "stats" },
      stat("Pending agents", agentCounts.pending, agentCounts.pending > 0),
      stat("Active agents", agentCounts.active),
      stat("Revoked agents", agentCounts.revoked),
      stat("Fleets", state.fleets.length),
      metrics ? stat("Active entries", metrics.entries.active) : null),
    pendingQueue(refresh),
    h("div", { class: "grid-2" },
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Active agents by trust level" })),
        h("div", { class: "card-body" }, bars(activeByLevel))),
      h("div", { class: "card" }, h("div", { class: "card-head" }, h("h2", { text: "Active agents per fleet" })),
        h("div", { class: "card-body" }, bars(agentsPerFleet)))));
}

async function viewAgents(root) {
  await loadAgentsAndFleets();
  const refresh = () => route();
  const search = h("input", { type: "search", placeholder: "Filter by name, owner or fleet…" });
  const statusFilter = h("select", {}, ["all", "pending", "active", "revoked"].map((s) => h("option", { value: s, text: s === "all" ? "All statuses" : s })));
  const holder = h("div");
  const render = () => {
    const q = search.value.trim().toLowerCase();
    const shown = state.agents.filter((a) =>
      (statusFilter.value === "all" || a.status === statusFilter.value) &&
      (!q || [a.name, a.owner_alias || "", fleetName(a.home_fleet_id)].some((v) => v.toLowerCase().includes(q))));
    holder.replaceChildren(table(
      ["Agent", "Status", "Trust level", "Home fleet", "Owner", "Activated", ""],
      shown.map((a) => h("tr", {},
        h("td", {}, h("strong", { text: a.name })),
        h("td", {}, statusBadge(a.status)),
        h("td", {}, a.status === "pending" ? h("span", { class: "muted", text: "—" }) : levelBadge(a.trust_level)),
        h("td", { text: fleetName(a.home_fleet_id) }),
        h("td", { text: a.owner_alias || "—" }),
        h("td", { title: fmtTime(a.activated_at), text: a.activated_at ? fmtAgo(a.activated_at) : "—" }),
        agentActions(a, refresh))),
      state.agents.length ? "No agents match the filter." : "No agents have registered yet."));
  };
  search.addEventListener("input", render);
  statusFilter.addEventListener("change", render);
  render();
  root.replaceChildren(
    pageHead("Agents", `${state.agents.length} registered`),
    h("div", { class: "card" }, h("div", { class: "card-head" }, h("div", { class: "filters" }, search, statusFilter)),
      h("div", { class: "card-body" }), holder));
}

async function viewFleets(root) {
  const [, metrics] = await Promise.all([loadAgentsAndFleets(), api("GET", "metrics").catch(() => null)]);
  const writes = (metrics && metrics.fleets.writes_by_fleet) || {};
  const name = h("input", { placeholder: "New fleet name", required: true });
  const create = h("button", { type: "submit", class: "btn btn-primary", text: "Create fleet" });
  const form = h("form", { class: "row" }, name, create);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const value = name.value.trim();
    if (!value) return;
    create.disabled = true;
    try {
      const fleet = await guarded(() => api("POST", "admin/fleets", { body: { name: value } }));
      if (fleet) { toast(`Fleet “${fleet.name}” created`); route(); }
    } catch (err) {
      toast(err.message, true);
    } finally {
      create.disabled = false;
    }
  });
  root.replaceChildren(
    pageHead("Fleets", "Fleets partition the pool. They cannot be deleted."),
    h("div", { class: "card" }, h("div", { class: "card-body" }, form)),
    h("div", { class: "card" }, table(
      ["Fleet", "Active agents", "Entries written", "Created", "Id"],
      state.fleets.map((f) => h("tr", {},
        h("td", {}, h("strong", { text: f.name })),
        h("td", { text: state.agents.filter((a) => a.status === "active" && a.home_fleet_id === f.id).length }),
        h("td", { text: writes[f.name] ?? "—" }),
        h("td", { title: fmtTime(f.created_at), text: fmtAgo(f.created_at) }),
        h("td", {}, h("code", { text: f.id })))),
      "No fleets yet. Create one before activating agents.")));
}

async function viewAudit(root) {
  await loadAgentsAndFleets().catch(() => undefined); // for fleet names only
  const actor = h("input", { type: "search", placeholder: "Actor (exact), e.g. admin:1a2b3c4d5e6f" });
  const action = h("select", {}, h("option", { value: "", text: "All actions" }), AUDIT_ACTIONS.map((a) => h("option", { value: a, text: a })));
  const since = h("input", { type: "datetime-local", title: "Since (local time)" });
  const apply = h("button", { type: "submit", class: "btn", text: "Apply" });
  const form = h("form", { class: "filters" }, actor, action, since, apply);
  const tbody = h("tbody");
  const more = h("button", { type: "button", class: "btn", text: "Load older" });
  const moreWrap = h("div", { class: "load-more" }, more);
  let cursor = null;

  // Fleet ids read better as names; the id stays in the tooltip.
  const target = (r) => {
    if (r.target && r.action === "fleet.create") {
      return h("td", { title: r.target, text: (r.detail && r.detail.name) || fleetName(r.target) });
    }
    return h("td", { text: r.target ?? "—" });
  };
  const detail = (r) => {
    const d = { ...(r.detail || {}) };
    if (r.action === "fleet.create") delete d.name;
    if (d.home_fleet_id) {
      d.home_fleet = fleetName(d.home_fleet_id);
      delete d.home_fleet_id;
    }
    if (r.action === "agent.home_fleet_set") { d.from = fleetName(d.from); d.to = fleetName(d.to); }
    return Object.keys(d).length ? JSON.stringify(d) : "";
  };
  const row = (r) => h("tr", {},
    h("td", { class: "nowrap", title: r.occurred_at, text: fmtTime(r.occurred_at) }),
    h("td", { class: "nowrap" }, r.actor_kind === "cli"
      ? h("span", { class: "badge badge-muted", title: "hivemind-keys CLI: the actor name is typed by the operator and not verified", text: "cli · unverified" })
      : h("span", { class: "badge badge-level", title: "a verified admin key, recorded by fingerprint", text: "admin key" })),
    h("td", {}, h("code", { text: r.actor })),
    h("td", { text: r.action }),
    target(r),
    h("td", { class: "detail", text: detail(r) }));

  const load = async (append) => {
    const query = {
      actor: actor.value.trim(),
      action: action.value,
      since: since.value ? new Date(since.value).toISOString() : "",
      limit: AUDIT_PAGE,
      before: append ? cursor : "",
    };
    const rows = await guarded(() => api("GET", "admin/audit-log", { query }));
    if (!rows) return;
    if (!append) tbody.replaceChildren();
    tbody.append(...rows.map(row));
    if (!append && rows.length === 0) {
      tbody.append(h("tr", {}, h("td", { class: "empty", colspan: 6, text: "No audit rows match." })));
    }
    cursor = rows.length ? rows[rows.length - 1].id : cursor;
    moreWrap.hidden = rows.length < AUDIT_PAGE;
  };
  form.addEventListener("submit", (event) => { event.preventDefault(); load(false).catch((err) => toast(err.message, true)); });
  more.addEventListener("click", () => load(true).catch((err) => toast(err.message, true)));

  root.replaceChildren(
    pageHead("Audit log", "Every admin action, newest first. CLI actors are claims typed by the operator, not verified identities."),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, form),
      h("div", { class: "card-body" }),
      h("div", { class: "table-wrap" }, h("table", {},
        h("thead", {}, h("tr", {}, ["When", "Kind", "Actor", "Action", "Target", "Detail"].map((t) => h("th", { text: t })))),
        tbody)),
      moreWrap));
  await load(false);
}

async function viewKeys(root) {
  const confirmInput = h("input", { placeholder: "type rotate to confirm", autocomplete: "off" });
  const rotate = h("button", { type: "submit", class: "btn btn-danger-solid", text: "Rotate the org key", disabled: true });
  confirmInput.addEventListener("input", () => { rotate.disabled = confirmInput.value.trim() !== "rotate"; });
  const form = h("form", { class: "row" }, confirmInput, rotate);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (confirmInput.value.trim() !== "rotate") return;
    rotate.disabled = true;
    try {
      const result = await guarded(() => api("POST", "admin/org-key/rotate"));
      if (result) {
        confirmInput.value = "";
        showKeyOnce("New org key", result.key, "Every previous org key has stopped working. Redistribute this one to everyone who registers agents.");
      }
    } catch (err) {
      toast(err.message, true);
    }
  });
  root.replaceChildren(
    pageHead("Org key", "The single key shared by the whole cluster. It gates registration only."),
    h("div", { class: "card card-danger" },
      h("div", { class: "card-head" }, h("h2", { text: "Rotate the org key" })),
      h("div", { class: "card-body" },
        h("p", { class: "callout callout-danger", text: "This is the cluster-wide kill switch: every existing org key stops working immediately. Agent keys are not affected." }),
        form)),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", { text: "Admin keys" })),
      h("div", { class: "card-body" },
        h("p", { class: "muted" }, "Admin keys are issued and revoked with the ", h("code", { text: "hivemind-keys" }),
          " CLI only (", h("code", { text: "issue-admin" }), " / ", h("code", { text: "revoke-admin" }),
          "), so a leaked admin key cannot mint more of itself."))));
}

// --- routing + session ---------------------------------------------------------------

const VIEWS = { overview: viewOverview, agents: viewAgents, fleets: viewFleets, audit: viewAudit, keys: viewKeys };

async function route() {
  if (!getKey() && !memoryKey) { showLogin(); return; }
  const name = (window.location.hash.replace(/^#\//, "") || "overview").split("?")[0];
  const view = VIEWS[name] || viewOverview;
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === (VIEWS[name] ? name : "overview")));
  const root = $("view");
  if (!root.firstChild) root.append(h("p", { class: "loading", text: "Loading…" }));
  try {
    await guarded(() => view(root));
  } catch (err) {
    root.replaceChildren(h("div", { class: "card" }, h("div", { class: "card-body" },
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

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", signIn);
  $("logout").addEventListener("click", () => signOut());
  $("modal").addEventListener("cancel", (event) => {
    if ($("modal").dataset.dismissable === "no") event.preventDefault();
  });
  $("modal").addEventListener("close", () => $("modal-body").replaceChildren());
  window.addEventListener("hashchange", route);
  if (getKey()) { showShell(); route(); checkApi(); } else { showLogin(); }
});
