// Hivemind admin panel: the Stats view (ADR 0029).
// Fleets, Agents and Users, rolled up from GET admin/activity (and the
// metrics counters). Observation for the operator only. Sortable, filterable.

import {
  h, state, loadAgentsAndFleets, fetchMetrics, fetchActivity, pageHead, refreshButton, windowSelector, card, unavailable,
  fmtNum, fmtAgo, fmtTime, fmtOf, link, memoriesHref, hooks, miniBar,
} from "./core.js";

let tab = "fleets";
const sorts = { fleets: { key: "recent_writes", dir: -1 }, agents: { key: "recent_writes", dir: -1 }, users: { key: "recent_writes", dir: -1 } };
let filterText = "";

const NONE = "(no owner alias)";

function laterOf(a, b) {
  if (!a) return b || null;
  if (!b) return a;
  return a > b ? a : b;
}

function fleetRows(activity, metrics) {
  const byFleet = (metrics && metrics.health && metrics.health.by_fleet) || {};
  return (activity.fleets || []).map((f) => ({
    name: f.fleet_id ? f.name : "(no fleet)",
    href: f.fleet_id ? memoriesHref({ fleet: f.fleet_id }) : null,
    agents: f.agents, entries: f.entries, active: f.active, writers: f.writers, recent_writes: f.recent_writes,
    last_write_at: f.last_write_at,
    health: byFleet[f.name] || null,
  }));
}

function agentRows(activity, metrics) {
  const flagged = (metrics && metrics.feedback && metrics.feedback.by_agent) || {};
  return (activity.agents || []).map((a) => ({
    name: a.name, href: memoriesHref({ author: a.name }), registered: a.registered, owner: a.owner_alias, fleet: a.home_fleet, status: a.status,
    entries: a.entries, active: a.active, recent_writes: a.recent_writes, last_write_at: a.last_write_at,
    verdicts_given: a.verdicts_given, recent_verdicts: a.recent_verdicts,
    helpful: a.received ? a.received.helpful : 0, stale: a.received ? a.received.stale : 0, wrong: a.received ? a.received.wrong : 0,
    confirmed: flagged[a.name] ? flagged[a.name].confirmed : null, flags: flagged[a.name] ? flagged[a.name].flags : null,
  }));
}

function userRows(activity) {
  const users = new Map();
  for (const a of activity.agents || []) {
    const owner = a.owner_alias || NONE;
    if (!users.has(owner)) {
      users.set(owner, { name: owner, href: null, agents: 0, active_agents: 0, entries: 0, recent_writes: 0, verdicts_given: 0, last_activity: null });
    }
    const u = users.get(owner);
    u.agents += 1;
    if (a.status === "active") u.active_agents += 1;
    u.entries += a.entries || 0;
    u.recent_writes += a.recent_writes || 0;
    u.verdicts_given += a.verdicts_given || 0;
    u.last_activity = laterOf(u.last_activity, laterOf(a.last_write_at, a.last_verdict_at));
  }
  return [...users.values()];
}

const num = (key) => ({ key, num: true, render: (r) => fmtNum(r[key]) });
const when = (key, label) => ({ key, label, render: (r) => h("span", { title: fmtTime(r[key]), text: r[key] ? fmtAgo(r[key]) : "—" }) });

function columns(which, rows) {
  const maxWrites = Math.max(1, ...rows.map((r) => r.recent_writes || 0));
  const writes = {
    key: "recent_writes", label: `Writes (${state.days}d)`, num: true,
    render: (r) => h("div", { class: "inline-bar" }, h("span", { text: fmtNum(r.recent_writes) }), miniBar(r.recent_writes || 0, maxWrites)),
  };
  const nameCol = (label) => ({
    key: "name", label,
    render: (r) => h("span", {}, r.href ? link(r.name, r.href, { class: "link strong" }) : h("strong", { text: r.name }),
      r.registered === false ? h("span", { class: "badge badge-muted", title: "Wrote or reported but is not a registered agent", text: "unregistered" }) : null),
  });
  if (which === "fleets") {
    return [nameCol("Fleet"), { ...num("agents"), label: "Agents" }, { ...num("entries"), label: "Entries" }, { ...num("active"), label: "Active" },
      { ...num("writers"), label: "Writers" }, writes, when("last_write_at", "Last write"),
      { key: "sourced", label: "Sourced", get: (r) => r.health && r.health.sourced_share ? r.health.sourced_share.share : null, render: (r) => (r.health ? fmtOf(r.health.sourced_share) : "—") },
      { key: "backlog", label: "Flag backlog", num: true, get: (r) => (r.health ? r.health.flagged_backlog_14d : null), render: (r) => (r.health ? fmtNum(r.health.flagged_backlog_14d) : "—") },
      { key: "floating", label: "Floating", num: true, get: (r) => (r.health ? r.health.floating_gap : null), render: (r) => (r.health ? fmtNum(r.health.floating_gap) : "—") }];
  }
  if (which === "agents") {
    return [nameCol("Agent"), { key: "owner", label: "Owner", render: (r) => r.owner || "—" }, { key: "fleet", label: "Home fleet", render: (r) => r.fleet || "—" },
      { ...num("entries"), label: "Entries" }, { ...num("active"), label: "Active" }, writes, when("last_write_at", "Last write"),
      { ...num("verdicts_given"), label: "Verdicts given" },
      { ...num("helpful"), label: "Helpful received" }, { ...num("stale"), label: "Stale received" }, { ...num("wrong"), label: "Wrong received" },
      { key: "confirmed", label: "Flags confirmed", num: true, render: (r) => (r.flags == null ? "—" : `${r.confirmed} of ${r.flags}`) }];
  }
  return [nameCol("Owner"), { ...num("agents"), label: "Agents" }, { ...num("active_agents"), label: "Active agents" }, { ...num("entries"), label: "Entries" },
    writes, { ...num("verdicts_given"), label: "Verdicts given" }, when("last_activity", "Last activity")];
}

function sortedTable(which, rows) {
  const cols = columns(which, rows);
  const sort = sorts[which];
  const q = filterText.trim().toLowerCase();
  const shown = rows.filter((r) => !q || [r.name, r.owner, r.fleet].some((v) => (v || "").toLowerCase().includes(q)));
  const val = (r, c) => (c.get ? c.get(r) : r[c.key]);
  const col = cols.find((c) => c.key === sort.key) || cols[0];
  shown.sort((a, b) => {
    const x = val(a, col);
    const y = val(b, col);
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * sort.dir;
  });
  const holder = h("div", { class: "table-wrap" });
  const head = cols.map((c) => {
    const active = c.key === sort.key;
    return h("th", { class: c.num ? "num" : "", "aria-sort": active ? (sort.dir > 0 ? "ascending" : "descending") : "none" },
      h("button", {
        type: "button", class: "sort", onclick: () => {
          sorts[which] = { key: c.key, dir: active ? -sort.dir : (c.num || c.key.includes("_at") || c.key === "last_activity" ? -1 : 1) };
          hooks.rerenderStats();
        },
      }, c.label, active ? h("span", { class: "sort-mark", "aria-hidden": "true", text: sort.dir > 0 ? " ▲" : " ▼" }) : null));
  });
  holder.append(h("table", { class: "stats-table" },
    h("thead", {}, h("tr", {}, head)),
    h("tbody", {}, shown.length ? shown.map((r) => h("tr", {}, cols.map((c) => h("td", { class: c.num ? "num" : "" }, c.render(r)))))
      : h("tr", {}, h("td", { class: "empty", colspan: cols.length, text: rows.length ? "Nothing matches the filter." : "Nothing recorded in this window." })))));
  return holder;
}

export async function viewStats(root) {
  const [, metrics, activity] = await Promise.all([loadAgentsAndFleets(), fetchMetrics(), fetchActivity(state.days)]);
  const render = () => {
    const tabs = h("div", { class: "tabs", role: "tablist" }, [["fleets", "Fleets"], ["agents", "Agents"], ["users", "Users"]].map(([id, label]) =>
      h("button", {
        type: "button", role: "tab", class: id === tab ? "tab active" : "tab", "aria-selected": id === tab ? "true" : "false", text: label,
        onclick: () => { tab = id; render(); },
      })));
    const filter = h("input", { type: "search", value: filterText, placeholder: "Filter…", "aria-label": "Filter rows" });
    filter.addEventListener("input", () => {
      filterText = filter.value;
      const pos = filter.selectionStart;
      render();
      const again = root.querySelector("input[type=search]");
      if (again) { again.focus(); again.setSelectionRange(pos, pos); }
    });
    let body;
    if (!activity) body = unavailable("Activity");
    else {
      const rows = tab === "fleets" ? fleetRows(activity, metrics) : tab === "agents" ? agentRows(activity, metrics) : userRows(activity);
      body = h("div", {}, sortedTable(tab, rows),
        tab === "users" ? h("p", { class: "muted small note", text: "Users are owners as named at registration (the owner alias); an alias is a claim, not a verified identity." }) : null);
    }
    root.replaceChildren(
      pageHead("Stats", `Writes and verdicts over the last ${state.days} days. For you only: nothing here ranks or reaches agents.`,
        windowSelector(() => hooks.refresh()), refreshButton()),
      h("div", { class: "card" }, h("div", { class: "card-head" }, tabs, filter), body));
  };
  hooks.rerenderStats = render;
  render();
}
