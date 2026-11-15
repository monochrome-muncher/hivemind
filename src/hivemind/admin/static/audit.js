// Hivemind admin panel: the audit log (ADR 0029).
// Everything API-sourced is rendered with textContent (see core.js).

import {
  api, guarded, h, toast, fleetName, fmtTime, loadAgentsAndFleets, pageHead, parseHash, optional, fill,
} from "./core.js";
import { openEntry } from "./drawer.js";
import { column } from "./csv.js";

const AUDIT_ACTIONS = [
  "agent.register", "agent.activate", "agent.trust_level_set", "agent.home_fleet_set", "agent.owner_set", "agent.role_set", "agent.role_clear", "agent.revoke",
  "fleet.create", "fleet.rename", "fleet.phase_set", "fleet.phase_clear", "fleet.merge", "fleet.unmerge", "fleet.move_agents", "fleet.move_undo", "org_key.rotate", "entry.withdraw", "entry.restore", "entry.redact", "entry.move_to_self", "entry.pin", "entry.unpin",
  "admin_key.issue", "admin_key.revoke", "agent_key.issue", "agent_key.replace", "agent_key.lock",
  "agent.cleanup", "agent.cleanup_undo", "agent.retire", "agent.retire_undo",
  "schema.rollback",
];
const AUDIT_PAGE = 50;
// Rows whose target is a memory: the id opens it in the drawer.
const ENTRY_ROW_ACTIONS = ["entry.withdraw", "entry.restore", "entry.redact", "entry.move_to_self"];
// Rows whose target is the fleet whose phase was set or cleared (ADR 0143).
const FLEET_PHASE_ACTIONS = ["fleet.phase_set", "fleet.phase_clear"];
// Rows whose target is the source fleet and whose detail names a target fleet.
const FLEET_ROW_ACTIONS = ["fleet.merge", "fleet.unmerge", "fleet.move_agents", "fleet.move_undo"];

export const AUDIT_HEADERS = ["When", "Kind", "Actor", "Action", "Target", "Detail"];

/** CSV columns for audit rows (the ADR 0027 shape; `detail` as JSON). */
export const auditCsvColumns = [
  column("occurred_at"), column("actor_kind"), column("actor"), column("action"), column("target"),
  column("detail", (r) => (r.detail && Object.keys(r.detail).length ? JSON.stringify(r.detail) : "")),
];

/** Builds the <tr> for one audit row; `nameOf(fleetId)` names a fleet (merged ones too). Fleet ids read better as names; the id stays in the tooltip. */
export function auditRow(nameOf) {
  const target = (r) => {
    if (r.target && r.action === "fleet.create") {
      return h("td", { title: r.target, text: (r.detail && r.detail.name) || fleetName(r.target) });
    }
    if (r.target && r.action === "fleet.rename") {
      return h("td", { title: r.target, text: (r.detail && r.detail.to) || nameOf(r.target) });
    }
    if (r.target && (FLEET_ROW_ACTIONS.includes(r.action) || FLEET_PHASE_ACTIONS.includes(r.action))) {
      return h("td", { title: r.target, text: nameOf(r.target) });
    }
    if (r.target && ENTRY_ROW_ACTIONS.includes(r.action)) {
      return h("td", {}, h("button", { type: "button", class: "linklike", title: "Open this memory", text: r.target, onclick: () => openEntry(r.target) }));
    }
    return h("td", { text: r.target ?? "—" });
  };
  const detail = (r) => {
    const d = { ...(r.detail || {}) };
    if (r.action === "entry.redact") {
      // The audit row never holds the wiped text: author, the admin's reason, and what to review.
      const n = d.feedback_notes || 0;
      const w = d.withdraw_reasons || 0;
      return `Redacted a memory by ${d.author || "unknown"}. Reason: ${d.reason || "none given"}. ` +
        `Left to review: ${n} feedback ${n === 1 ? "note" : "notes"}, ${w} withdrawal ${w === 1 ? "reason" : "reasons"}.`;
    }
    if (r.action === "fleet.create") delete d.name;
    if (d.home_fleet_id) {
      d.home_fleet = fleetName(d.home_fleet_id);
      delete d.home_fleet_id;
    }
    if (r.action === "agent.home_fleet_set") { d.from = fleetName(d.from); d.to = fleetName(d.to); }
    if (FLEET_ROW_ACTIONS.includes(r.action)) {
      d.target_fleet = d.target_fleet_name || nameOf(d.target_fleet_id);
      delete d.target_fleet_id;
      delete d.target_fleet_name;
    }
    return Object.keys(d).length ? JSON.stringify(d) : "";
  };
  const actorBadge = (kind) => {
    if (kind === "cli") {
      return h("span", { class: "badge badge-muted", title: "hivemind-keys CLI: the actor name is typed by the operator and not verified", text: "cli · unverified" });
    }
    if (kind === "org_key") {
      return h("span", { class: "badge badge-muted", title: "a registration with the shared org key: the fingerprint names the key, not who used it", text: "org key" });
    }
    if (kind === "agent_key") {
      return h("span", { class: "badge badge-level", title: "an agent's own key, recorded by its verified name", text: "agent key" });
    }
    return h("span", { class: "badge badge-level", title: "a verified admin key, recorded by fingerprint", text: "admin key" });
  };
  const row = (r) => h("tr", {},
    h("td", { class: "nowrap", title: r.occurred_at, text: fmtTime(r.occurred_at) }),
    h("td", { class: "nowrap" }, actorBadge(r.actor_kind)),
    h("td", {}, h("code", { text: r.actor })),
    h("td", { text: r.action }),
    target(r),
    h("td", { class: "detail", text: detail(r) }));
  return row;
}

/** A datetime-local value (local time, to the minute) for an ISO time in a hash; empty when it is not one. */
function localTimeValue(iso) {
  const d = new Date(iso || "");
  if (!iso || Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export async function viewAudit(root) {
  await loadAgentsAndFleets().catch(() => undefined); // for fleet names only
  // A merged fleet is not in state.fleets, but audit rows still name it.
  const everyFleet = (await optional(() => api("GET", "admin/fleets", { query: { include_merged: "true" } }))) || [];
  const nameOf = (id) => {
    const fleet = everyFleet.find((f) => f.id === id);
    return fleet ? fleet.name : fleetName(id);
  };
  const preset = parseHash().params;
  const actor = h("input", { type: "search", placeholder: "Actor (exact), e.g. admin:1a2b3c4d5e6f", value: preset.get("actor") || "" });
  const subject = h("input", { type: "search", placeholder: "About agent (exact name)", value: preset.get("subject") || "",
    title: "Rows about this agent: its own actions, admin actions on it, withdrawals of its memories, fleet moves and retirements that name it" });
  const target = h("input", { type: "search", placeholder: "Target (exact), e.g. an agent name or entry id", value: preset.get("target") || "" });
  const action = h("select", {}, h("option", { value: "", text: "All actions" }), AUDIT_ACTIONS.map((a) => h("option", { value: a, text: a })));
  if (AUDIT_ACTIONS.includes(preset.get("action"))) action.value = preset.get("action");
  const since = h("input", { type: "datetime-local", title: "Since (local time)", value: localTimeValue(preset.get("since")) });
  const apply = h("button", { type: "submit", class: "btn", text: "Apply" });
  const form = h("form", { class: "filters" }, subject, actor, target, action, since, apply);
  const tbody = h("tbody");
  const more = h("button", { type: "button", class: "btn", text: "Load older" });
  const moreWrap = h("div", { class: "load-more" }, more);
  let cursor = null;

  const row = auditRow(nameOf);

  const load = async (append) => {
    const query = {
      actor: actor.value.trim(),
      subject: subject.value.trim(),
      target: target.value.trim(),
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

  fill(root, 
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
