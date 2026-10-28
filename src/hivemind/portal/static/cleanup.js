// Hivemind agent portal: cleaning up the agent's own work over a time window,
// and undoing it (ADR 0118). The admin panel's cleanup (ADR 0111) limited to the
// signed-in agent. Everything API-sourced is rendered with textContent (h()).
// It always previews first (dry_run) and asks for the agent's name before it
// applies. A cleanup withdraws memories and sets ratings aside; it deletes
// nothing, and it writes no memory.

import {
  api, h, state, guarded, fleetName, pageHead, refreshButton, card, fill, table, toast,
  openModal, closeModal, fmtAgo, fmtNum, fmtTime, kindBadge,
} from "./session.js";
import { openEntry } from "./drawer.js";

const pad = (n) => String(n).padStart(2, "0");
/** A Date as the value of a datetime-local input (local time). */
const localValue = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const plural = (n, one, many = `${one}s`) => `${fmtNum(n)} ${n === 1 ? one : many}`;

const entryRow = (e) => h("li", {},
  kindBadge(e.kind),
  h("span", {},
    h("a", { href: "#", class: "link", text: e.summary, onclick: (ev) => { ev.preventDefault(); openEntry(e.id, () => {}); } }), " ",
    h("span", { class: "muted small", text: `${e.author} · ${e.scope}${e.fleet_id ? ` · ${fleetName(e.fleet_id)}` : ""} · written ${fmtAgo(e.created_at)}` })));

function withdrawBlock(preview) {
  return h("div", { class: "field" },
    h("label", { text: `Memories to withdraw (${fmtNum(preview.withdraw_total)})` }),
    h("span", { class: "hint", text: "Active memories written in the window, in any scope, by this agent or by agents retired in its favour. They are withdrawn as a cleanup and taken off any fleet briefing. An undo puts them back." }),
    preview.withdraw.length
      ? h("div", { class: "modal-scroll cleanup-list" }, h("ul", { class: "merge-list" }, preview.withdraw.map(entryRow)))
      : h("span", { class: "muted small", text: "None." }),
    preview.withdraw_truncated
      ? h("span", { class: "hint", text: `Showing the newest ${fmtNum(preview.withdraw.length)}. All ${fmtNum(preview.withdraw_total)} are withdrawn.` })
      : null,
    preview.pins_total ? h("span", { class: "hint", text: `${plural(preview.pins_total, "pin")} on these memories will be removed.` }) : null);
}

function ratingsBlock(preview) {
  const parts = Object.entries(preview.ratings_by_verdict).map(([v, n]) => `${fmtNum(n)} ${v}`);
  return h("div", { class: "field" },
    h("label", { text: `Verdicts to set aside (${fmtNum(preview.ratings_total)})` }),
    h("span", { class: "hint", text: "The helpful, stale and wrong verdicts this agent last gave in the window stop counting towards rankings and flags. The rows are kept, and giving a verdict on the same memory again brings one back." }),
    preview.ratings_total ? h("span", { class: "small", text: parts.join(", ") }) : h("span", { class: "muted small", text: "None." }));
}

/** The restorable memories, each ticked by default. */
function restoreBlock(preview) {
  const boxes = preview.restorable.map((e) => ({ e, box: h("input", { type: "checkbox", value: e.id, checked: true }) }));
  const count = h("strong");
  const sync = () => { count.textContent = `${boxes.filter((b) => b.box.checked).length} of ${fmtNum(preview.restorable_total)}`; };
  for (const b of boxes) b.box.addEventListener("change", sync);
  sync();
  const items = boxes.map(({ e, box }) => h("li", {},
    h("label", { class: "check" }, box,
      h("span", {}, h("strong", { text: e.summary }), " ",
        h("span", { class: "muted small", text: `${e.author} · ${e.scope} · written ${fmtAgo(e.created_at)}` })))));
  return {
    el: h("div", { class: "field" },
      h("label", {}, "Older memories to bring back: ", count),
      h("span", { class: "hint", text: "Memories that these memories replaced, including through a chain of replacements, and that nobody has replaced since. A ticked one becomes active again. An undo replaces it again." }),
      boxes.length ? h("div", { class: "modal-scroll cleanup-list" }, h("ul", { class: "merge-list" }, items)) : h("span", { class: "muted small", text: "None." }),
      preview.restorable_truncated
        ? h("span", { class: "hint", text: `Showing the newest ${fmtNum(boxes.length)} of ${fmtNum(preview.restorable_total)}; the others stay superseded.` })
        : null),
    chosen: () => boxes.filter((b) => b.box.checked).map((b) => b.e.id),
  };
}

/** Ask for the agent's name, then apply the previewed cleanup. */
function confirmDialog(me, preview, restore, onDone) {
  const confirm = h("input", { id: "cl-confirm", autocomplete: "off", placeholder: me.name, "aria-label": `Type ${me.name} to confirm` });
  const ids = restore.chosen();
  openModal({
    title: `Clean up ${me.name}'s work?`,
    body: [
      h("p", {}, `${plural(preview.withdraw_total, "memory", "memories")} will be withdrawn, ${plural(preview.ratings_total, "verdict")} set aside and ${plural(ids.length, "older memory", "older memories")} brought back. Nothing is deleted, and you can undo it from this page.`),
      h("div", { class: "field" }, h("label", { for: "cl-confirm", text: `Type “${me.name}” to confirm` }), confirm),
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Clean up",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          if (confirm.value.trim() !== me.name) throw new Error(`Type “${me.name}” to confirm.`);
          const body = {
            since: preview.since,
            until: preview.until,
            dry_run: false,
            restore_ids: ids,
            expected_withdraw: preview.withdraw_total,
            expected_ratings: preview.ratings_total,
          };
          const done = await guarded(() => api("POST", "portal/cleanup", { body }));
          if (!done) return;
          closeModal();
          const bits = [`${plural(done.withdrawn, "memory", "memories")} withdrawn`, `${plural(done.ratings_set_aside, "verdict")} set aside`];
          if (done.restored) bits.push(`${plural(done.restored, "memory", "memories")} brought back`);
          toast(`Cleaned up: ${bits.join(", ")}`);
          onDone();
        },
      },
    ],
  });
}

/** Ask before undoing one of the agent's cleanups. */
function undoDialog(cleanup, onDone) {
  openModal({
    title: "Undo this cleanup?",
    body: [h("p", {}, "Memories the cleanup withdrew, and that are still withdrawn, become active again. Memories it brought back are replaced again where that still fits, its verdicts count again, and pins are restored where the fleet has room.")],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Undo cleanup",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          const done = await guarded(() => api("POST", `portal/cleanups/${encodeURIComponent(cleanup.id)}/undo`));
          if (!done) return;
          closeModal();
          const left = [];
          if (done.reactivate_skipped) left.push(`${plural(done.reactivate_skipped, "memory", "memories")} not re-activated`);
          if (done.resupersede_skipped) left.push(`${plural(done.resupersede_skipped, "brought-back memory", "brought-back memories")} not replaced again`);
          if (done.pins_skipped.length) left.push(`${plural(done.pins_skipped.length, "pin")} not restored`);
          toast(`Undone: ${plural(done.reactivated, "memory", "memories")} active again, ${plural(done.ratings_cleared, "verdict")} counting again${left.length ? `; ${left.join(", ")} (changed since, or no room)` : ""}`);
          onDone();
        },
      },
    ],
  });
}

function cleanupsCard(cleanups, onChange) {
  const rows = cleanups.map((c) => h("tr", {},
    h("td", { class: "nowrap", title: fmtTime(c.cleaned_at), text: fmtAgo(c.cleaned_at) }),
    h("td", { class: "small", text: `${fmtTime(c.since)} to ${fmtTime(c.until)}` }),
    h("td", { class: "num", text: c.withdrawn }),
    h("td", { class: "num", text: c.ratings_set_aside }),
    h("td", { class: "num", text: c.restored }),
    h("td", { text: c.run_by === "agent" ? "this agent" : "an admin" }),
    h("td", { class: "actions" },
      c.undone_at
        ? h("span", { class: "muted small", title: fmtTime(c.undone_at), text: `undone ${fmtAgo(c.undone_at)}` })
        : (c.undoable
          ? h("button", { type: "button", class: "btn btn-sm", text: "Undo", onclick: () => undoDialog(c, onChange) })
          : h("span", { class: "muted small", title: c.undo_blocked || "", text: "cannot undo here" })))));
  return card("Past cleanups", table(["When", "Window", "Withdrawn", "Verdicts set aside", "Brought back", "Run by", ""], rows, "Nothing has been cleaned up yet."), { flush: true });
}

export async function viewCleanup(root) {
  const me = state.me;
  const since = h("input", { type: "datetime-local", id: "cl-since", value: localValue(new Date(Date.now() - 24 * 3600 * 1000)) });
  const until = h("input", { type: "datetime-local", id: "cl-until", title: "Leave empty for now" });
  const previewButton = h("button", { type: "button", class: "btn btn-sm", text: "Preview" });
  const applyButton = h("button", { type: "button", class: "btn btn-danger-solid", text: "Clean up…", hidden: true });
  const holder = h("div");
  const previewCard = h("div", { class: "card", hidden: true }, h("div", { class: "card-body" }, holder, h("div", { class: "row" }, applyButton)));
  const listHolder = h("div");
  let current = null; // { preview, restore }
  let ticket = 0;

  const bounds = () => {
    if (!since.value) throw new Error("Choose when the window starts.");
    const body = { since: new Date(since.value).toISOString() };
    if (until.value) body.until = new Date(until.value).toISOString();
    return body;
  };
  const reset = () => {
    ticket += 1;
    current = null;
    applyButton.hidden = true;
    previewCard.hidden = true;
    holder.replaceChildren();
  };
  const run = async () => {
    const mine = ++ticket;
    current = null;
    applyButton.hidden = true;
    previewCard.hidden = false;
    let window;
    try { window = bounds(); } catch (err) { holder.replaceChildren(h("p", { class: "form-error", text: err.message })); return; }
    holder.replaceChildren(h("p", { class: "muted", text: "Looking at what it wrote and rated…" }));
    try {
      const preview = await guarded(() => api("POST", "portal/cleanup", { body: { ...window, dry_run: true } }));
      if (mine !== ticket || !preview) return;
      const restore = restoreBlock(preview);
      current = { preview, restore };
      holder.replaceChildren(
        h("p", { class: "small muted", text: `Window: ${fmtTime(preview.since)} to ${fmtTime(preview.until)}. Memories count by when they were written, verdicts by when they were last changed.` }),
        ...(preview.nothing_to_clean ? [h("p", { class: "callout small", text: "Nothing was written or rated in this window." })] : []),
        withdrawBlock(preview), ratingsBlock(preview), restore.el);
      applyButton.hidden = preview.nothing_to_clean;
    } catch (err) {
      if (mine === ticket) holder.replaceChildren(h("p", { class: "form-error", text: err.message }));
    }
  };
  const loadList = async () => {
    const cleanups = await guarded(() => api("GET", "portal/cleanups"));
    if (cleanups) listHolder.replaceChildren(cleanupsCard(cleanups, () => { loadList(); reset(); }));
  };
  since.addEventListener("input", reset);
  until.addEventListener("input", reset);
  previewButton.addEventListener("click", run);
  applyButton.addEventListener("click", () => {
    if (current) confirmDialog(me, current.preview, current.restore, () => { loadList(); reset(); });
  });

  fill(root,
    pageHead("Clean up", `For when ${me.name} wrote or rated badly for a while. Pick a window, look at what would change, then clean up. Nothing is deleted. Only a cleanup run here, and only the newest one, can be undone from this page.`, refreshButton()),
    card(null, h("div", { class: "field" },
      h("label", { for: "cl-since", text: "From (local time)" }), since,
      h("label", { for: "cl-until", text: "Until (empty is now)" }), until,
      h("div", { class: "row" }, previewButton)), { className: "card-filters" }),
    previewCard,
    listHolder);
  await loadList();
}
