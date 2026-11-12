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
// Restoring a withdrawn memory is the same: only the admin side passes
// restoreEntry, so the agent portal never shows it (ADR 0124).
// Likewise the pin hooks (pinStatus, pinEntry, unpinEntry): without pinStatus
// the drawer shows no pin controls (ADR 0116). Redacting a withdrawn memory
// (ADR 0125) is likewise offered only when the app passes redactEntry: the
// admin panel does, the agent portal never does.

import {
  $, h, toast, openModal, closeModal, fmtTime, fmtAgo, kindBadge, stateBadge, copyText, link, verdictBadge, chipButton,
} from "./ui.js";

const config = {
  fetchEntry: async () => { throw new Error("The memory drawer is not configured."); },
  withdrawEntry: null, // (id, reason) => truthy when done
  canWithdraw: () => true, // (entry) => whether to offer Withdraw
  reasonRequired: false, // whether a withdrawal needs a reason
  redactEntry: null, // (id, reason, confirm) => the answer ({feedback_notes, flag_notes}) when done (ADR 0125)
  restoreEntry: null, // (id, reason) => truthy when restored; without it there is no Restore button
  pinStatus: null, // async (entry) => "pinnable" | "pinned" | null (no pin controls)
  pinEntry: null, // (id) => truthy when pinned
  unpinEntry: null, // (id) => the answer ({removed}) when done
  pinNote: "", // a line shown under the pin button
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
let chipActions = {}; // {onTag, onEntity}: what a click on a tag or entity chip does
let showToken = 0; // only the latest show() renders
const stack = []; // ids opened in a chain, so "Back" works

/**
 * Open the drawer on an entry. `changed` runs after a withdraw. With `onTag(name)` and
 * `onEntity(name)` the tag and entity chips are buttons that close the drawer and call them.
 */
export function openEntry(id, changed, { onTag = null, onEntity = null } = {}) {
  onChange = changed || (() => {});
  chipActions = { onTag, onEntity };
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

// A chip that filters by `name` when the app handles it, else plain text.
const drawerChip = (text, name, onPick, title) => (onPick
  ? chipButton(text, () => { closeDrawer(); onPick(name); }, { title: title || `Only memories with ${name}` })
  : h("span", { class: "chip", title, text }));

const entryButton = (item, text) => h("button", {
  type: "button", class: "linklike", text, onclick: () => openEntry(item.id, onChange, chipActions),
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

  const redact = e.state !== "active" && !e.redacted_at && config.redactEntry
    ? h("button", { type: "button", class: "btn btn-danger", text: "Redact…", onclick: () => redactDialog(e) })
    : null;
  const restore = e.state === "withdrawn" && !e.redacted_at && config.restoreEntry
    ? h("button", { type: "button", class: "btn", text: "Restore…", onclick: () => restoreDialog(e) })
    : null;

  return h("div", { class: "entry" },
    h("div", { class: "row entry-top" }, back, kindBadge(e.kind), stateBadge(e.state),
      e.redacted_at ? h("span", { class: "badge badge-muted", title: e.redacted_at, text: `Redacted ${fmtAgo(e.redacted_at)}` }) : null,
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
      e.withdrawn_at ? h("p", {}, h("span", { class: "muted", text: "Withdrawn " }),
        h("span", { title: e.withdrawn_at, text: `${fmtTime(e.withdrawn_at)} · ${fmtAgo(e.withdrawn_at)}` })) : null,
      e.withdrawn_reason ? h("p", {}, h("span", { class: "muted", text: "Withdrawn: " }), h("span", { class: "plain", text: e.withdrawn_reason })) : null) : null,

    e.tags && e.tags.length ? h("section", { class: "drawer-sec" }, h("h3", { text: "Tags" }),
      h("div", { class: "chips" }, e.tags.map((t) => drawerChip(t, t, chipActions.onTag)))) : null,
    e.entities && e.entities.length ? h("section", { class: "drawer-sec" }, h("h3", { text: "Entities" }),
      h("div", { class: "chips" }, e.entities.map((en) => drawerChip(`${en.name} · ${en.kind}`, en.name, chipActions.onEntity, en.kind)))) : null,
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

    pinSection(e),

    withdraw ? h("section", { class: "drawer-sec drawer-danger" },
      h("h3", { text: "Withdraw" }),
      h("p", { class: "muted small", text: "Withdrawing removes this memory from every agent's search. It stays here and in the audit log." }),
      withdraw) : null,

    restore ? h("section", { class: "drawer-sec" },
      h("h3", { text: "Restore" }),
      h("p", { class: "muted small", text: "Restoring makes this memory active again, so agents can find it. Pins are not restored. If an agent cleanup withdrew it, undo that cleanup instead." }),
      restore) : null,

    redact ? h("section", { class: "drawer-sec drawer-danger" },
      h("h3", { text: "Redact" }),
      h("p", { class: "muted small", text: "Wipes the text of this withdrawn or superseded memory for good, for a leaked secret or personal data. It cannot be undone." }),
      redact) : null);
}

// The pin controls fill in once the app says whether this entry is pinned.
function pinSection(e) {
  if (!config.pinStatus || !config.pinEntry || !config.unpinEntry) return null;
  const sec = h("section", { class: "drawer-sec", hidden: true });
  const mine = showToken;
  Promise.resolve(config.pinStatus(e)).then((status) => {
    if (mine !== showToken || !status) return;
    const pinned = status === "pinned";
    const error = h("p", { class: "form-error small", hidden: true });
    const button = h("button", {
      type: "button", class: "btn", text: pinned ? "Unpin" : "Pin to the fleet",
      onclick: async () => {
        button.disabled = true;
        error.hidden = true;
        try {
          const done = await (pinned ? config.unpinEntry(e.id) : config.pinEntry(e.id));
          if (!done) return;
          toast(!pinned ? "Memory pinned" : done.removed === false ? "It was already unpinned" : "Memory unpinned");
          await show(e.id);
          onChange();
        } catch (err) {
          error.textContent = err.message;
          error.hidden = false;
        } finally {
          button.disabled = false;
        }
      },
    });
    sec.append(
      h("h3", { text: "Pin" }),
      h("p", { class: "muted small", text: pinned ? "This memory is pinned: every member of the fleet reads it first." : config.pinNote }),
      button, error);
    sec.hidden = false;
  }).catch(() => {});
  return sec;
}

function redactDialog(e) {
  const reason = h("textarea", {
    id: "f-redact-reason", rows: "3", maxlength: "500", required: true,
    placeholder: "What did this memory hold? Do not repeat the secret here.",
  });
  const typed = h("input", { id: "f-redact-confirm", autocomplete: "off", spellcheck: "false", placeholder: e.id });
  openModal({
    title: "Redact this memory?",
    body: [
      h("p", { class: "plain", text: "This wipes the summary, body, tags, sources, payload, extracted entities and links of this memory, and its embedding and keyword index. It cannot be undone and the text cannot be brought back." }),
      h("p", { class: "muted small", text: "The author, scope, fleet, dates, feedback and the audit log stay. Feedback notes written by agents and withdrawal reasons in the audit log are not edited: they may quote the text, and the confirmation says how many to review. Backups still hold the text until they age out." }),
      h("div", { class: "field" }, h("label", { for: "f-redact-reason", text: "Reason (kept in the audit log, so do not repeat the secret)" }), reason),
      h("div", { class: "field" }, h("label", { for: "f-redact-confirm", text: "Type the memory id to confirm" }),
        h("div", { class: "row tight" }, h("code", { text: e.id })), typed),
    ],
    actions: [
      { label: "Cancel" },
      {
        label: "Redact for good",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          const text = reason.value.trim();
          if (!text) throw new Error("Say why it is being redacted: the reason is kept in the audit log.");
          if (typed.value.trim() !== e.id) throw new Error("Type the memory id exactly to confirm.");
          const done = await config.redactEntry(e.id, text, typed.value.trim());
          if (!done) return;
          closeModal();
          const n = done.feedback_notes || 0;
          const w = done.withdraw_reasons || 0;
          const todo = [];
          if (n) todo.push(`${n} feedback ${n === 1 ? "note" : "notes"} (${done.flag_notes || 0} flag notes)`);
          if (w) todo.push(`${w} withdrawal ${w === 1 ? "reason" : "reasons"} in the audit log`);
          toast(todo.length ? `Memory redacted. Review ${todo.join(" and ")}: they may quote the text.` : "Memory redacted");
          await show(e.id);
          onChange();
        },
      },
    ],
  });
}

function restoreDialog(e) {
  const reason = h("textarea", {
    id: "f-restore-reason", rows: "3", maxlength: "500", required: true,
    placeholder: "Why is this being restored?",
  });
  openModal({
    title: "Restore this memory?",
    body: [
      h("p", { class: "plain", text: e.summary }),
      h("p", { class: "muted small", text: "It becomes active again and every agent can find it. Pins it had are not restored. The reason is kept in the audit log." }),
      h("div", { class: "field" }, h("label", { for: "f-restore-reason", text: "Reason" }), reason),
    ],
    actions: [
      { label: "Cancel" },
      {
        label: "Restore",
        class: "btn-primary",
        submit: true,
        onClick: async () => {
          const text = reason.value.trim();
          if (!text) throw new Error("Say why it is being restored: the reason is kept in the audit log.");
          const done = await config.restoreEntry(e.id, text);
          if (!done) return;
          closeModal();
          toast("Memory restored");
          await show(e.id);
          onChange();
        },
      },
    ],
  });
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
      h("p", { class: "muted small", text: "It disappears from every agent's search. The reason is kept with the entry, and only an admin can restore it." }),
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

