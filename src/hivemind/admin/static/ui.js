// Hivemind front-end kit: the generic half of the panel, with no session,
// no key and no admin route in it, so another front end (an agent-key app)
// can import it as is.
//
// Rules every module that uses it keeps:
// * Every piece of API data reaches the page through textContent (the
//   h() helper) or createElementNS + textContent (the svg() helper), and
//   never through an HTML-parsing sink, so an agent name, an owner alias or
//   an entry written by an agent cannot inject markup. The CSP forbids
//   inline script as a second line. Entry text is untrusted data (ADR 0043).
// * A freshly issued key is shown once, in the modal, and never stored.
// * No link href is ever built from API data other than an in-panel hash.

export const LEVELS = [
  { value: 0, name: "untrusted", hint: "no access (dormant)" },
  { value: 1, name: "lurker", hint: "own + home-fleet read, own write" },
  { value: 2, name: "contributor", hint: "+ home-fleet write" },
  { value: 3, name: "privileged", hint: "+ read across all fleets" },
];
export const KINDS = ["fact", "insight", "decision"];
export const VERDICTS = ["helpful", "stale", "wrong"];
export const SCOPES = ["self", "fleet", "org"];

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

/** Replace `el`'s children, skipping null/false (replaceChildren would print "null"). */
export function fill(el, ...children) {
  el.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return el;
}

/** Parse `#/view?a=b&c=d` into {view, params}. */
export function parseHash(hash = window.location.hash) {
  const raw = hash.replace(/^#\/?/, "");
  const [view, query = ""] = raw.split("?");
  return { view: view || "overview", params: new URLSearchParams(query) };
}

/** An in-panel hash route to `view` with the given query (empty values dropped). */
export function viewHref(view, filters = {}) {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) {
    if (v !== undefined && v !== null && v !== "" && v !== false) params.set(k, String(v));
  }
  const q = params.toString();
  return `#/${view}${q ? "?" + q : ""}`;
}

/** An in-panel hash link to Memories with the given filters. */
export const memoriesHref = (filters = {}) => viewHref("memories", filters);

/** An in-panel hash link to the Feedback log (reporter, verdict, author, fleet, entry). */
export const feedbackHref = (filters = {}) => viewHref("feedback", filters);

/** An in-panel hash link to one agent's page. */
export const agentHref = (name) => viewHref("agent", { name });

/** An <a> to an in-panel hash route (the href is built from our own params only). */
export function link(text, href, props = {}) {
  return h("a", { href, class: "link", ...props }, text);
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
const VERDICT_BADGE = { helpful: "badge-active", stale: "badge-pending", wrong: "badge-revoked" };
export const verdictBadge = (verdict, text = verdict) => h("span", { class: `badge ${VERDICT_BADGE[verdict] || "badge-muted"}`, title: verdict, text });
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
