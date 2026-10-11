// Hivemind admin panel (ADR 0029): shared core.
//
// Rules this module (and every module of the panel) keeps:
// * Every piece of API data reaches the page through textContent (the
//   h() helper) or createElementNS + textContent (the svg() helper), and
//   never through an HTML-parsing sink, so an agent name, an owner alias or
//   an entry written by an agent cannot inject markup. The CSP forbids
//   inline script as a second line. Entry text is untrusted data (ADR 0043).
// * The admin key lives in sessionStorage for this tab only. It is sent
//   as X-API-Key to the same-origin proxy and nowhere else.
// * A freshly issued key is shown once, in the modal, and never stored.
// * No link href is ever built from API data other than an in-panel hash.

export const KEY_STORAGE = "hivemind.adminKey";
export const LEVELS = [
  { value: 0, name: "untrusted", hint: "no access (dormant)" },
  { value: 1, name: "lurker", hint: "own + home-fleet read, own write" },
  { value: 2, name: "contributor", hint: "+ home-fleet write" },
  { value: 3, name: "privileged", hint: "+ read across all fleets" },
];
export const KINDS = ["fact", "insight", "decision"];
export const SCOPES = ["self", "fleet", "org"];
export const WINDOWS = [7, 30, 90];

// --- tiny DOM helpers ------------------------------------------------------------

/** Build an element. Strings and numbers become text nodes (never HTML). */
export function h(tag, props = {}, ...children) {
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

const SVG_NS = "http://www.w3.org/2000/svg";

/** Build an SVG element (createElementNS; text only through `text`). */
export function svg(tag, props = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") el.textContent = value;
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key === "class" ? "class" : key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child);
  }
  return el;
}

export const $ = (id) => document.getElementById(id);

// --- navigation hooks (set by admin.js, so modules need no import cycle) --------------

export const hooks = {
  refresh: () => {},
  rerenderStats: () => {},
  signOut: () => {},
};

/** Parse `#/view?a=b&c=d` into {view, params}. */
export function parseHash(hash = window.location.hash) {
  const raw = hash.replace(/^#\/?/, "");
  const [view, query = ""] = raw.split("?");
  return { view: view || "overview", params: new URLSearchParams(query) };
}

/** An in-panel hash link to Memories with the given filters. */
export function memoriesHref(filters = {}) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "" && v !== false) params.set(k, String(v));
  }
  const q = params.toString();
  return `#/memories${q ? "?" + q : ""}`;
}

/** An <a> to an in-panel hash route (the href is built from our own params only). */
export function link(text, href, props = {}) {
  return h("a", { href, class: "link", ...props }, text);
}

// --- session + API -------------------------------------------------------------------

let memoryKey = null; // fallback when sessionStorage is unavailable

export function getKey() {
  try { return sessionStorage.getItem(KEY_STORAGE); } catch { return null; }
}
export function setKey(key) {
  try { sessionStorage.setItem(KEY_STORAGE, key); } catch { /* private mode */ }
  memoryKey = key;
}
export function clearKey() {
  try { sessionStorage.removeItem(KEY_STORAGE); } catch { /* ignore */ }
  memoryKey = null;
}
export const hasKey = () => Boolean(getKey() || memoryKey);

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

/** Like api(), but resolves to {payload, headers}. */
export async function apiRaw(method, path, { body, query, key } = {}) {
  const url = new URL(`/v1/${path}`, window.location.origin);
  for (const [k, v] of Object.entries(query || {})) {
    if (v !== undefined && v !== null && v !== "" && v !== false) url.searchParams.set(k, v);
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
  return { payload, headers: response.headers };
}

export async function api(method, path, options) {
  return (await apiRaw(method, path, options)).payload;
}

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

// --- formatting -----------------------------------------------------------------------

export const levelName = (value) => (LEVELS.find((l) => l.value === value) || { name: String(value) }).name;

export function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
}

export function fmtAgo(iso) {
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

export const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString());
export const fmtPct = (share) => (share == null ? "—" : `${Math.round(share * 1000) / 10}%`);
export const fmtOf = (x) => (x && x.total ? `${fmtPct(x.share)} (${x.count} of ${x.total})` : "—");

export function fmtDuration(seconds) {
  if (seconds == null) return "—";
  if (seconds >= 86400) return `${Math.round(seconds / 864) / 100} days`;
  if (seconds >= 3600) return `${Math.round(seconds / 36) / 100} hours`;
  return `${Math.round(seconds / 60)} min`;
}

export const statusBadge = (status) => h("span", { class: `badge badge-${status}`, text: status });
export const levelBadge = (value) => h("span", { class: "badge badge-level", text: `${value} · ${levelName(value)}` });
export const kindBadge = (kind) => h("span", { class: `badge badge-kind badge-kind-${kind}`, text: kind });
export const stateBadge = (st) => h("span", {
  class: `badge ${st === "active" ? "badge-active" : st === "withdrawn" ? "badge-revoked" : "badge-muted"}`, text: st,
});

export function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.className = isError ? "toast toast-error" : "toast";
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, isError ? 6000 : 3500);
}

/** Copy text to the clipboard; returns whether it worked. */
export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

// --- modal ------------------------------------------------------------------------------

/**
 * Open the shared modal. `actions` are {label, class, onClick} where
 * onClick may return a promise; a thrown error is shown in the modal and
 * keeps it open.
 */
export function openModal({ title, body, actions, dismissable = true }) {
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
  const first = modal.querySelector("input, select, textarea");
  if (first) first.focus();
}

export function closeModal() {
  const modal = $("modal");
  $("modal-body").replaceChildren(); // drop any key from the DOM
  if (modal.open) modal.close();
}

/** The one place a raw key is ever shown. It is not stored anywhere. */
export function showKeyOnce(title, key, note) {
  const box = h("div", { class: "key-box", text: key });
  const copy = h("button", { type: "button", class: "btn btn-sm", text: "Copy" });
  copy.addEventListener("click", async () => {
    if (await copyText(key)) {
      copy.textContent = "Copied";
    } else {
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

// --- shared layout pieces ------------------------------------------------------------------------

export function table(headers, rows, emptyText, { className = "" } = {}) {
  return h("div", { class: "table-wrap" },
    h("table", { class: className },
      h("thead", {}, h("tr", {}, headers.map((t) => (t instanceof Node ? t : h("th", { text: t }))))),
      h("tbody", {}, rows.length ? rows : h("tr", {}, h("td", { class: "empty", colspan: headers.length, text: emptyText })))));
}

export function pageHead(title, subtitle, ...extra) {
  return h("div", { class: "page-head" },
    h("div", {}, h("h1", { text: title }), subtitle ? h("p", { class: "muted", text: subtitle }) : null),
    h("div", { class: "page-actions" }, ...extra));
}

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

export function card(title, body, { head = [], className = "", flush = false } = {}) {
  return h("div", { class: `card ${className}`.trim() },
    title || head.length ? h("div", { class: "card-head" }, title ? h("h2", { text: title }) : null, ...head) : null,
    flush ? body : h("div", { class: "card-body" }, body));
}

export const unavailable = (what = "This") => h("p", { class: "muted small note", text: `${what} is not available from this API version.` });

export function stat(label, value, { warn = false, hint = null } = {}) {
  return h("div", { class: warn ? "stat stat-warn" : "stat" },
    h("div", { class: "stat-label", text: label }),
    h("div", { class: "stat-value", text: value }),
    hint ? h("div", { class: "stat-hint muted small", text: hint }) : null);
}

/** A small inline bar (CSSOM width, allowed by the CSP). */
export function miniBar(value, max, className = "") {
  const fill = h("div", { class: `bar-fill ${className}`.trim() });
  fill.style.width = `${max ? Math.max(value ? 4 : 0, Math.round((value / max) * 100)) : 0}%`;
  return h("div", { class: "bar-track bar-inline", title: String(value) }, fill);
}

export function bars(counts) {
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
