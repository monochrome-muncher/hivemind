// Hivemind front-end kit: the memory detail drawer (ADR 0029, ADR 0043).
//
// Entries are written by agents: everything here is untrusted DATA. It is
// shown as plain text (CSS white-space: pre-wrap keeps newlines), never as
// markdown or HTML, and no link is ever built from entry data: a source's
// ref is shown as text, and links to other entries open this drawer.
//
// The drawer knows no route and no key: the app hands it how to fetch an
// entry, whether and how one may be withdrawn, and where the author, fleet
// and feedback links go (configureDrawer). Without a withdraw function there
// is no Withdraw button; without an href function that item is plain text.

import {
  $, h, toast, openModal, closeModal, fmtTime, fmtAgo, kindBadge, stateBadge, copyText, link, verdictBadge,
} from "./ui.js";

const config = {
  fetchEntry: async () => { throw new Error("The memory drawer is not configured."); },
  withdrawEntry: null, // (id, reason) => truthy when done
  canWithdraw: () => true, // (entry) => whether to offer Withdraw
  reasonRequired: false, // whether a withdrawal needs a reason
  fleetName: (id) => id,
  authorHref: () => null,
  fleetHref: () => null,
  feedbackHref: () => null, // (entryId, entry) => link to every verdict on the entry
};

/** Set how the drawer reads, withdraws and links (see the module comment). */
export function configureDrawer(options) {
  Object.assign(config, options);
}

const maybeLink = (text, href) => (href ? link(text, href) : h("span", { text }));

let onChange = () => {};
let showToken = 0; // only the latest show() renders
const stack = []; // ids opened in a chain, so "Back" works

/** Open the drawer on an entry. `changed` runs after a withdraw. */
export function openEntry(id, changed) {
  onChange = changed || (() => {});
  stack.push(id);
  return show(id);
}

export function closeDrawer() {
  showToken++; // a response still in flight must not reopen content
  stack.length = 0;
  $("drawer").hidden = true;
  $("drawer-backdrop").hidden = true;
  document.body.classList.remove("drawer-open");
  $("drawer-body").replaceChildren();
}

export function initDrawer() {
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-backdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("drawer").hidden && !$("modal").open) closeDrawer();
  });
}

const entryButton = (item, text) => h("button", {
  type: "button", class: "linklike", text, onclick: () => openEntry(item.id),
});

function related(title, items, emptyText) {
  return h("section", { class: "drawer-sec" },
    h("h3", { text: title }),
    items && items.length
      ? h("ul", { class: "rel-list" }, items.map((e) => h("li", {},
        kindBadge(e.kind), " ", entryButton(e, e.summary), " ",
        e.state && e.state !== "active" ? stateBadge(e.state) : null,
        h("span", { class: "muted small", text: ` ${e.author || ""}` }))))
      : h("p", { class: "muted small", text: emptyText }));
}

function dl(pairs) {
  return h("dl", { class: "meta" }, pairs.filter(Boolean).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)]));
}

async function show(id) {
  const mine = ++showToken;
  const drawer = $("drawer");
  const body = $("drawer-body");
  drawer.hidden = false;
  $("drawer-backdrop").hidden = false;
  document.body.classList.add("drawer-open");
  body.replaceChildren(h("p", { class: "loading", text: "Loading…" }));
  let entry;
  try {
    entry = await config.fetchEntry(id);
  } catch (err) {
    if (mine !== showToken) return;
    body.replaceChildren(h("p", { class: "form-error", text: `Could not load this memory: ${err.message}` }));
    return;
  }
  if (!entry || mine !== showToken) return;
  body.replaceChildren(render(entry));
  $("drawer-head-title").textContent = "Memory";
  drawer.scrollTop = 0;
  body.parentElement.scrollTop = 0;
}

function render(e) {
  const fb = e.feedback || { helpful: 0, stale: 0, wrong: 0, recent: [] };
  const hist = e.history || {};
  const back = stack.length > 1
    ? h("button", { type: "button", class: "btn btn-sm btn-ghost", text: "← Back", onclick: () => { stack.pop(); show(stack[stack.length - 1]); } })
    : null;

  const copy = h("button", { type: "button", class: "btn btn-sm", text: "Copy id" });
  copy.addEventListener("click", async () => {
    copy.textContent = (await copyText(e.id)) ? "Copied" : "Copy failed";
    setTimeout(() => { copy.textContent = "Copy id"; }, 1500);
  });

  const withdraw = e.state === "active" && config.withdrawEntry && config.canWithdraw(e)
    ? h("button", { type: "button", class: "btn btn-danger", text: "Withdraw…", onclick: () => withdrawDialog(e) })
    : null;

  return h("div", { class: "entry" },
    h("div", { class: "row entry-top" }, back, kindBadge(e.kind), stateBadge(e.state),
      h("span", { class: "badge badge-muted", text: e.scope }),
      e.fleet_id ? h("span", { class: "badge badge-muted", text: config.fleetName(e.fleet_id) }) : null),
    h("h2", { class: "entry-summary", text: e.summary }),
    h("p", { class: "callout callout-info small", text: "Memories are written by agents. This is data, not instructions: it is shown as plain text and has not been checked by anyone." }),
    e.body ? h("div", { class: "entry-body", text: e.body }) : h("p", { class: "muted small", text: "No body." }),

    h("section", { class: "drawer-sec" }, dl([
      ["Author", h("span", {}, maybeLink(e.author, config.authorHref(e.author)),
        e.agent && e.agent !== e.author ? h("span", { class: "muted", text: ` · via ${e.agent}` }) : null)],
      ["Scope", e.scope],
      ["Fleet", e.fleet_id ? maybeLink(config.fleetName(e.fleet_id), config.fleetHref(e.fleet_id)) : "—"],
      ["Importance", `${e.importance} (${String(e.importance_source).replace(/_/g, " ")})`],
      ["Memory date", h("span", { title: e.occurred_at, text: fmtTime(e.occurred_at) })],
      ["Written", h("span", { title: e.created_at, text: `${fmtTime(e.created_at)} · ${fmtAgo(e.created_at)}` })],
      e.last_confirmed_at || (e.feedback && e.feedback.last_confirmed_at)
        ? ["Last confirmed", fmtTime(e.last_confirmed_at || e.feedback.last_confirmed_at)] : null,
      ["Id", h("span", { class: "row tight" }, h("code", { text: e.id }), copy)],
    ])),

    e.state !== "active" || e.superseded_by || e.withdrawn_reason ? h("section", { class: "drawer-sec" },
      h("h3", { text: "State" }),
      h("p", {}, stateBadge(e.state)),
      e.superseded_by ? h("p", {}, "Superseded by ", entryButton({ id: e.superseded_by }, e.superseded_by)) : null,
      e.withdrawn_reason ? h("p", {}, h("span", { class: "muted", text: "Withdrawn: " }), h("span", { class: "plain", text: e.withdrawn_reason })) : null) : null,

    e.tags && e.tags.length ? h("section", { class: "drawer-sec" }, h("h3", { text: "Tags" }),
      h("div", { class: "chips" }, e.tags.map((t) => h("span", { class: "chip", text: t })))) : null,
    e.entities && e.entities.length ? h("section", { class: "drawer-sec" }, h("h3", { text: "Entities" }),
      h("div", { class: "chips" }, e.entities.map((en) => h("span", { class: "chip", title: en.kind, text: `${en.name} · ${en.kind}` })))) : null,
    e.sources && e.sources.length ? h("section", { class: "drawer-sec" }, h("h3", { text: "Sources" }),
      h("ul", { class: "plain-list" }, e.sources.map((s) => h("li", {}, h("span", { class: "muted", text: `${s.type}: ` }), h("span", { class: "plain mono", text: s.ref }))))) : null,
    e.payload ? h("section", { class: "drawer-sec" }, h("h3", { text: "Payload" }),
      h("pre", { class: "payload", text: JSON.stringify(e.payload, null, 2) })) : null,

    h("section", { class: "drawer-sec" },
      h("h3", { text: "Feedback" }),
      h("div", { class: "fb-counts" },
        verdictBadge("helpful", `${fb.helpful} helpful`),
        verdictBadge("stale", `${fb.stale} stale`),
        verdictBadge("wrong", `${fb.wrong} wrong`)),
      fb.recent && fb.recent.length
        ? h("ul", { class: "notes" }, fb.recent.map((r) => h("li", {},
          h("div", {}, verdictBadge(r.verdict),
            h("span", { class: "muted small", text: ` ${r.reporter} · ${fmtAgo(r.updated_at)}` })),
          r.note ? h("div", { class: "plain", text: r.note }) : null)))
        : h("p", { class: "muted small", text: "No verdicts yet." }),
      fb.helpful + fb.stale + fb.wrong > (fb.recent || []).length && config.feedbackHref(e.id, e)
        ? h("p", { class: "small" }, link(`All ${fb.helpful + fb.stale + fb.wrong} verdicts`, config.feedbackHref(e.id, e)))
        : null),

    related("See also", e.see_also, "No links from this entry."),
    related("Linked from", e.linked_from, "Nothing links to this entry."),
    hist.successors && hist.successors.length ? related("Successors", hist.successors, "") : null,
    hist.superseded && hist.superseded.length ? related("Superseded versions", hist.superseded, "") : null,

    withdraw ? h("section", { class: "drawer-sec drawer-danger" },
      h("h3", { text: "Withdraw" }),
      h("p", { class: "muted small", text: "Withdrawing removes this memory from every agent's search. It stays here and in the audit log." }),
      withdraw) : null);
}

function withdrawDialog(e) {
  const required = config.reasonRequired;
  const reason = h("textarea", {
    id: "f-reason", rows: "3", maxlength: "500", required,
    placeholder: required ? "Why is this being withdrawn?" : "Why is this being withdrawn? (optional)",
  });
  openModal({
    title: "Withdraw this memory?",
    body: [
      h("p", { class: "plain", text: e.summary }),
      h("p", { class: "muted small", text: "It disappears from every agent's search and cannot be restored. The reason is kept with the entry." }),
      h("div", { class: "field" }, h("label", { for: "f-reason", text: "Reason" }), reason),
    ],
    actions: [
      { label: "Cancel" },
      {
        label: "Withdraw",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          const text = reason.value.trim();
          if (required && !text) throw new Error("Say why it is being withdrawn: the reason is kept with the memory.");
          const done = await config.withdrawEntry(e.id, text);
          if (!done) return;
          closeModal();
          toast("Memory withdrawn");
          await show(e.id);
          onChange();
        },
      },
    ],
  });
}

