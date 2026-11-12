// Hivemind agent portal (ADR 0095): the views. Each lists what the agent's
// key can read through the portal routes, with the admin panel's shared
// tables (lists.js) and memory drawer (drawer.js).

import {
  api, apiRaw, h, state, guarded, entryById, entryIdIn, parseHash, fleetName, levelName, pageHead, refreshButton, card, fill, table, toast, link, viewHref, canPin, PIN_REMINDER,
  stat, fmtAgo, fmtTime, kindBadge, stateBadge, verdictBadge,
  KINDS, SCOPES, VERDICTS,
} from "./session.js";
import { openEntry } from "./drawer.js";
import { PIN_HEADERS, cursorPages, entryTable, feedbackTable, flaggedTable, pagedTable, pinRow } from "./lists.js";
import { column } from "./csv.js";
import { filterFields, memoryFilterFields } from "./memfilters.js";

const select = (label, values, selected, anyText) => h("select", { "aria-label": label },
  h("option", { value: "", text: anyText }),
  values.map((v) => h("option", { value: v, text: v, selected: v === selected })));

/** Keep the view's hash in step with its filters (unless the user navigated away). */
function syncHash(root, view, filters) {
  if (root.dataset.superseded) return;
  const next = viewHref(view, filters);
  if (window.location.hash !== next) history.replaceState(null, "", next);
}

// --- its memories ----------------------------------------------------------------------

// What a successor also owns: the memories of the agents it took over from (ADR 0113).
const inherited = (me) => {
  const names = me.inherits_from || [];
  if (!names.length) return "";
  const who = names.length === 1 ? names[0] : `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
  return `, plus what ${who} wrote before ${names.length === 1 ? "it was" : "they were"} retired`;
};

/** When the key was last used. The stamp is written at most once an hour, so it is only good to the hour. */
const keyLastUsed = (iso) => {
  if (!iso) return { value: "Not recorded", hint: "No use of this key has been recorded yet." };
  const minutes = (Date.now() - new Date(iso).getTime()) / 60000;
  return {
    value: minutes < 60 ? "Within the last hour" : fmtAgo(iso),
    hint: `${fmtTime(iso)}. Recorded at most once an hour.`,
  };
};

/** Who this key is: the agent behind it, how far it is trusted, and when the key last worked. */
function statusCard(me) {
  const used = keyLastUsed(me.key_last_used_at);
  const inheritedFrom = me.inherits_from || [];
  return card("This agent", [
    h("div", { class: "stats" },
      stat("Agent", me.name),
      stat("Status", me.status || "unknown"),
      stat("Trust level", levelName(me.trust_level)),
      stat("Home fleet", me.home_fleet_id ? fleetName(me.home_fleet_id) : "None"),
      stat("Key last used", used.value, { hint: used.hint }),
      inheritedFrom.length ? stat("Also owns the memories of", inheritedFrom.join(", ")) : null),
    h("p", { class: "muted small", text: "If the last-used time looks wrong, for example the key was used when this agent was idle, someone else may hold the key. Ask your admin to replace it." })], { className: "status-card" });
}

export async function viewMemories(root) {
  const me = state.me;
  const { params } = parseHash();
  const kind = select("Kind", KINDS, params.get("kind") || "", "Any kind");
  const memoryFilters = memoryFilterFields((k) => params.get(k) || "", "all");
  const form = h("form", { class: "filters" }, kind, memoryFilters.state, memoryFilters.more);

  let applied = null;
  const chipFilters = {
    onTag: (name) => { memoryFilters.setTag(name); memories.load(true); },
    onEntity: (name) => { memoryFilters.setEntity(name); memories.load(true); },
  };
  const memories = entryTable({
    fetchPage: ({ limit, offset, fresh }) => {
      if (fresh || !applied) {
        applied = { kind: kind.value, ...memoryFilters.read() };
        syncHash(root, "memories", applied);
      }
      return guarded(() => api("GET", "portal/entries", {
        query: { author: me.name, kind: applied.kind, ...filterFields(applied), limit, offset },
      }));
    },
    onOpen: (id) => openEntry(id, () => memories.load(true), chipFilters),
    onTag: chipFilters.onTag,
    fleetName,
    emptyText: "It has not written any memories that match.",
    csvName: `${me.name}-memories`,
    statusText: (n) => `${n} shown, newest first`,
  });
  for (const el of [kind, ...memoryFilters.changeTargets()]) el.addEventListener("change", () => memories.load(true));
  form.addEventListener("submit", (event) => { event.preventDefault(); memories.load(true); });

  fill(root,
    statusCard(me),
    pageHead("Its memories", `Everything ${me.name} wrote${inherited(me)}, newest first, withdrawn and superseded ones included unless you pick a state. Open one to read it in full or withdraw it.`, refreshButton()),
    card(null, form, { className: "card-filters" }),
    memories.el);
  await memories.load(true);
}

// --- verdicts it gave / verdicts on its memories ------------------------------------------

function feedbackView(direction) {
  return async (root) => {
    const me = state.me;
    const given = direction === "given";
    const view = given ? "given" : "received";
    const { params } = parseHash();
    const verdict = select("Verdict", VERDICTS, params.get("verdict") || "", "Any verdict");
    // One memory's verdicts (from the drawer's "all verdicts" link): a removable chip.
    let entry = given ? "" : params.get("entry") || "";
    const entryChip = h("span", { class: "chip chip-filter", hidden: !entry });
    const paintEntry = () => {
      entryChip.hidden = !entry;
      entryChip.replaceChildren(entry ? `One memory: ${entry.slice(0, 8)}…` : "", " ",
        h("button", { type: "button", class: "linklike", "aria-label": "Show every memory", text: "×", onclick: () => { entry = ""; paintEntry(); list.load(true); } }));
    };
    paintEntry();
    const form = h("form", { class: "filters" }, verdict, entryChip);

    let applied = null;
    const list = feedbackTable({
      fetchPage: ({ limit, offset, fresh }) => {
        if (fresh || !applied) {
          applied = { verdict: verdict.value, entry };
          syncHash(root, view, applied);
        }
        return guarded(() => api("GET", "portal/feedback", {
          query: { direction, verdict: applied.verdict, entry_id: applied.entry, limit, offset },
        }));
      },
      onOpen: (id) => openEntry(id, () => list.load(true)),
      fleetName,
      emptyText: given ? "No verdict it gave matches, on memories it can still read." : "No verdict on its memories matches.",
      csvName: `${me.name}-verdicts-${direction}`,
    });
    verdict.addEventListener("change", () => list.load(true));
    form.addEventListener("submit", (event) => { event.preventDefault(); list.load(true); });

    fill(root,
      pageHead(given ? "Verdicts it gave" : "Verdicts on its memories",
        given
          ? `Every helpful, stale or wrong verdict ${me.name} gave, newest first, on memories it can still read. A later verdict on the same memory replaces the earlier one.`
          : `What other agents said about ${me.name}'s memories${inherited(me)}, newest first. A stale or wrong verdict usually means the memory needs a new version.`,
        refreshButton()),
      card(null, form, { className: "card-filters" }),
      list.el,
      h("p", { class: "muted small note", text: "Notes are written by agents: shown as plain text, not checked by anyone." }));
    await list.load(true);
  };
}

export const viewGiven = feedbackView("given");
export const viewReceived = feedbackView("received");

// --- memories that need attention -----------------------------------------------------------

export async function viewAttention(root) {
  const me = state.me;
  const list = flaggedTable({
    fetchPage: ({ limit, offset }) => guarded(() => api("GET", "portal/flagged", { query: { limit, offset } })),
    onOpen: (id) => openEntry(id, () => list.load(true)),
    fleetName,
    emptyText: "Nothing needs attention: none of its active memories carries a stale or wrong verdict.",
    csvName: `${me.name}-needs-attention`,
    owners: false,
  });
  fill(root,
    pageHead("Needs attention",
      `Active memories of ${me.name}${inherited(me)} that another agent rated stale or wrong, the longest-flagged first. Open one to read the verdicts. If it is out of date, withdraw it with a reason, or ask the agent to supersede it with a corrected memory.`,
      refreshButton()),
    list.el);
  await list.load(true);
}

// --- memories it relied on that changed (ADR 0138) ---------------------------------------------

const RELIED_HEADERS = ["Memory", "Kind", "Author", "What changed", "Rated helpful", "Verdicts now"];

/** The change as plain sentences, one per thing that happened since the agent rated the memory. */
export function changeSentences(row) {
  const change = row.change;
  const out = [];
  if (change.superseded) out.push({ text: "Superseded by a newer version", successor: change.superseded_by });
  if (change.withdrawn) {
    const when = change.withdrawn_at ? ` ${fmtAgo(change.withdrawn_at)}` : "";
    out.push({ text: row.withdrawn_reason ? `Withdrawn${when}: ${row.withdrawn_reason}` : `Withdrawn${when}` });
  }
  if (change.flagged_since) {
    const { stale, wrong } = change.flagged_since;
    const parts = [stale ? `stale ${stale}` : null, wrong ? `wrong ${wrong}` : null].filter(Boolean);
    out.push({ text: `Rated ${parts.join(", ")} since` });
  }
  if (!out.length) out.push({ text: "Changed since it was rated" });
  return out;
}

const reliedChangeCell = (row, open) => h("td", { class: "cell-note" },
  changeSentences(row).map((c) => h("div", {},
    h("span", { class: "plain", text: c.text }),
    c.successor ? [" ", h("button", { type: "button", class: "linklike", text: "Open the newer version", onclick: () => open(c.successor) })] : null)));

const reliedRow = (row, open) => h("tr", {},
  h("td", { class: "cell-summary" },
    h("button", { type: "button", class: "linklike summary-text", text: row.summary, onclick: () => open(row.id) }),
    row.state !== "active" ? h("div", {}, stateBadge(row.state)) : null),
  h("td", {}, kindBadge(row.kind)),
  h("td", { class: "nowrap", text: row.author }),
  reliedChangeCell(row, open),
  row.change.rated_helpful_at
    ? h("td", { class: "nowrap", title: fmtTime(row.change.rated_helpful_at), text: fmtAgo(row.change.rated_helpful_at) })
    : h("td", { class: "muted", text: "—" }),
  h("td", { class: "nowrap" },
    ...["helpful", "stale", "wrong"].map((v) => (row.feedback && row.feedback[v] ? [verdictBadge(v, row.feedback[v]), " "] : [])).flat()));

/** The memories the agent rated helpful that were superseded, withdrawn or flagged since. */
export async function viewRelied(root) {
  const me = state.me;
  const open = (id) => openEntry(id, () => list.load(true));
  const list = pagedTable({
    headers: RELIED_HEADERS,
    className: "entries",
    fetchPage: ({ limit, offset }) => guarded(() => api("GET", "portal/relied-on", { query: { limit, offset } })),
    renderRow: (row) => reliedRow(row, open),
    emptyText: "Nothing it rated helpful has changed since.",
    statusText: (n) => `${n} shown, newest memory first`,
    csv: {
      columns: [
        column("id"), column("kind"), column("summary"), column("author"),
        column("changed", (r) => changeSentences(r).map((c) => c.text).join("; ")),
        column("superseded_by", (r) => r.change.superseded_by),
        column("rated_helpful_at", (r) => r.change.rated_helpful_at),
        column("helpful", (r) => (r.feedback || {}).helpful || 0),
        column("stale", (r) => (r.feedback || {}).stale || 0),
        column("wrong", (r) => (r.feedback || {}).wrong || 0),
      ],
      name: `${me.name}-relied-on`,
      key: (r) => r.id,
      fetchPage: ({ limit, offset }) => guarded(() => api("GET", "portal/relied-on", { query: { limit, offset } })),
    },
  });
  fill(root,
    pageHead("Relied on, since changed",
      `Memories ${me.name} rated helpful that have changed since: replaced by someone else's newer version, withdrawn, or rated stale or wrong by someone else. Its own work may rest on something out of date. Read what changed, tell the agent to re-check it, or have it rate the memory again: helpful if it still holds, stale or wrong if not, and it leaves this list.`,
      refreshButton()),
    list.el,
    h("p", { class: "muted small note", text: "Withdrawal reasons are written by whoever withdrew the memory: shown as plain text, not checked by anyone." }));
  await list.load(true);
}

// --- history (ADR 0123) --------------------------------------------------------------------

const HISTORY_HEADERS = ["When", "By", "What happened", "Reason", "Memory"];

/** Who did it: "an admin" (never named), "this agent", or a predecessor by name. */
const historyBy = (row, me) => {
  if (row.by === "admin") return "an admin";
  if (row.by !== "agent" || !row.agent) return "another agent";
  return row.agent === me.name ? "this agent" : row.agent;
};

/** What the history says happened to the agent and the agents it took over from (ADR 0113), newest first. */
export async function viewHistory(root) {
  const me = state.me;
  const open = (id) => openEntry(id, () => list.load(true));
  const renderRow = (row) => h("tr", {},
    h("td", { class: "nowrap", title: fmtTime(row.occurred_at), text: fmtAgo(row.occurred_at) }),
    h("td", { class: "nowrap", text: historyBy(row, me) }),
    h("td", {}, row.summary,
      row.about && row.about !== me.name ? h("div", { class: "muted small", text: `about ${row.about}, which it took over from` }) : null),
    h("td", { class: "cell-note" }, row.reason ? h("div", { class: "plain", text: row.reason }) : h("span", { class: "muted", text: "—" })),
    h("td", { class: "nowrap" }, row.entry_id
      ? h("button", { type: "button", class: "linklike", text: "Open memory", onclick: () => open(row.entry_id) })
      : h("span", { class: "muted", text: "—" })));
  // The table and the CSV export each keep their own cursors, so an export never shifts the table's paging.
  const pages = () => cursorPages(({ limit, before }) => guarded(() => api("GET", "portal/history", { query: { limit, before } })));
  const list = pagedTable({
    headers: HISTORY_HEADERS,
    className: "entries history-log",
    fetchPage: pages(),
    renderRow,
    emptyText: "Nothing has been recorded about this agent yet.",
    statusText: (n) => `${n} shown, newest first`,
    csv: {
      columns: [
        column("occurred_at"), column("by", (r) => historyBy(r, me)), column("about"), column("action"),
        column("summary"), column("reason"), column("entry_id"),
      ],
      name: `${me.name}-history`,
      key: (r) => r.id,
      fetchPage: pages(),
    },
  });
  fill(root,
    pageHead("History",
      `What happened to ${me.name}${inherited(me)}, newest first: what it did itself and what an admin did to it. An admin is never named. A fleet is named only if this key can read it, and a memory opens only if this key can still read it.`,
      refreshButton()),
    list.el,
    h("p", { class: "muted small note", text: "Withdrawal reasons are written by whoever withdrew the memory: shown as plain text, not checked by anyone." }));
  await list.load(true);
}

// --- home fleet ---------------------------------------------------------------------------

export async function viewFleet(root) {
  const me = state.me;
  if (!me.home_fleet_id) {
    fill(root, pageHead("Home fleet", null),
      card(null, h("p", { class: "muted", text: `${me.name} has no home fleet yet. The admin sets one when it activates the agent.` })));
    return;
  }
  const pinsCard = h("div", {});
  const loadPins = async () => {
    const pins = await guarded(() => api("GET", "pins"));
    if (!pins) return;
    const open = (id) => openEntry(id, () => { loadPins(); memories.load(true); });
    // One call, then reload the list; a refusal (the caps) shows as a toast in the service's words.
    const act = (label, method, done) => (pin) => h("button", {
      type: "button", class: "btn btn-sm", text: label,
      onclick: async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const answer = await guarded(() => api(method, `portal/entries/${encodeURIComponent(pin.id)}/pin`));
          if (answer) {
            toast(done(answer));
            await loadPins();
          }
        } catch (err) {
          toast(err.message, true);
          button.disabled = false;
        }
      },
    });
    const unpin = act("Unpin", "DELETE", (a) => (a.removed === false ? "It was already unpinned" : "Memory unpinned"));
    const repin = act("Re-pin", "PUT", () => "Memory pinned again");
    const actions = (pin) => h("span", { class: "row tight" }, pin.changed_since_pin ? repin(pin) : null, unpin(pin));
    const mayPin = canPin();
    pinsCard.replaceChildren(card(`Pinned (${pins.pins.length})`,
      table(mayPin ? [...PIN_HEADERS, ""] : PIN_HEADERS,
        pins.pins.map((p) => pinRow(p, { onOpen: open, extra: mayPin ? actions : null })),
        "Nothing is pinned in this fleet.", { className: "entries" }),
      { flush: true }),
      h("p", { class: "muted small", text: mayPin ? `${PIN_REMINDER} Open a memory below to pin it.` : PIN_REMINDER }));
  };
  const memories = entryTable({
    fetchPage: ({ limit, offset }) => guarded(() => api("GET", "portal/entries", {
      query: { fleet_id: me.home_fleet_id, scope: "fleet", limit, offset },
    })),
    onOpen: (id) => openEntry(id, () => memories.load(true)),
    fleetName,
    emptyText: "Nobody has written to this fleet yet.",
    csvName: `${fleetName(me.home_fleet_id)}-fleet-memories`,
  });

  fill(root,
    pageHead(fleetName(me.home_fleet_id), "The active memories of its home fleet, newest first, and what the fleet pinned for every member to read.", refreshButton()),
    pinsCard,
    h("section", { class: "agent-sec" }, h("div", { class: "section-head" }, h("h2", { text: "Fleet memories" })), memories.el));
  await Promise.all([loadPins().catch((err) => toast(err.message, true)), memories.load(true)]);
}

// --- search -------------------------------------------------------------------------------

export async function viewSearch(root) {
  const me = state.me;
  const { params } = parseHash();
  const get = (k) => params.get(k) || "";
  const q = h("input", { type: "search", value: get("q"), placeholder: "Search what it can read, or paste a memory id…", "aria-label": "Search, or paste a memory id" });
  const kind = select("Kind", KINDS, get("kind"), "Any kind");
  const scope = select("Scope", SCOPES, get("scope"), "Any scope");
  const author = h("input", { type: "text", value: get("author"), placeholder: "Author (agent name)", "aria-label": "Author" });
  const memoryFilters = memoryFilterFields(get, "active");
  const explain = h("input", { type: "checkbox", id: "s-explain" });
  explain.checked = get("explain") === "1";
  const go = h("button", { type: "submit", class: "btn btn-primary", text: "Search" });
  const form = h("form", { class: "filters filters-wide" }, q, kind, scope, author, memoryFilters.state,
    h("label", { class: "check", for: "s-explain" }, explain, " Explain ranking"), go, memoryFilters.more);
  const degraded = h("p", { class: "callout callout-warn small", hidden: true, text: "The embedder is unavailable: results are keyword-only." });

  let applied = null;
  const chipFilters = {
    onTag: (name) => { memoryFilters.setTag(name); results.load(true); },
    onEntity: (name) => { memoryFilters.setEntity(name); results.load(true); },
  };
  const results = entryTable({
    fetchPage: async ({ limit, offset, fresh }) => {
      if (fresh || !applied) {
        applied = { q: q.value.trim(), kind: kind.value, scope: scope.value, author: author.value.trim(), ...memoryFilters.read(), explain: explain.checked ? "1" : "" };
        syncHash(root, "search", applied);
      }
      const f = applied;
      if (!f.q) return [];
      // A memory id reads that memory through the drawer's own read, which
      // the key's visibility bounds: one the key may not read is the same
      // 404 as one that does not exist. The other filters do not apply.
      const id = entryIdIn(f.q);
      if (id) {
        degraded.hidden = true;
        if (offset > 0) return [];
        return entryById(() => guarded(() => api("GET", `entries/${encodeURIComponent(id)}`)));
      }
      const body = { query: f.q, limit, offset };
      if (f.kind) body.kind = f.kind;
      if (f.scope) body.scope = f.scope;
      if (f.author) body.author = f.author;
      Object.assign(body, filterFields(f));
      if (f.explain) body.explain = true;
      const res = await guarded(() => apiRaw("POST", "portal/search", { body }));
      if (!res) return undefined;
      // A slow earlier search must not set the banner over a newer result.
      if (offset === 0 && applied === f) degraded.hidden = (res.headers.get("X-Hivemind-Degraded") || "") !== "keyword-only";
      return res.payload || [];
    },
    onOpen: (id) => openEntry(id, () => results.load(true), chipFilters),
    onTag: chipFilters.onTag,
    fleetName,
    emptyText: "Type a search or a memory id above, or nothing it can read matches.",
    csvName: `${me.name}-search`,
    statusText: (n) => {
      if (!applied || !applied.q) return "";
      if (entryIdIn(applied.q)) return n ? "The memory with this id (the filters do not apply to an id)" : "";
      return `${n} result${n === 1 ? "" : "s"}, best match first`;
    },
  });
  form.addEventListener("submit", (event) => { event.preventDefault(); results.load(true); });
  for (const el of memoryFilters.changeTargets()) el.addEventListener("change", () => results.load(true));

  fill(root,
    pageHead("Search", `Ranks like ${me.name}'s own search, over what its key can read; a memory id finds that memory if the key can read it. Searching here does not count in the fleet's search numbers.`,
      link("Its memories", viewHref("memories"), { class: "link small" })),
    card(null, form, { className: "card-filters" }),
    degraded,
    results.el);
  await results.load(true);
}
