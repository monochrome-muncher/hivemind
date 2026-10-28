// Hivemind admin panel: cleaning up after a bad agent, and undoing it (ADR 0111).
// Everything API-sourced is rendered with textContent (see core.js). The dialog
// always previews first (dry_run) and asks for the agent's name before it applies.
// A cleanup withdraws memories and sets ratings aside; it deletes nothing, and
// it writes no memory.

import {
  api, guarded, h, hooks, toast, openModal, closeModal, fmtAgo, fmtNum, fmtTime, fleetName, card, table, kindBadge,
} from "./core.js";
import { openEntry } from "./drawer.js";

const pad = (n) => String(n).padStart(2, "0");
/** A Date as the value of a datetime-local input (local time). */
const localValue = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const plural = (n, one, many = `${one}s`) => `${fmtNum(n)} ${n === 1 ? one : many}`;

const entryRow = (e, extra = []) => h("li", {},
  kindBadge(e.kind),
  h("span", {},
    h("a", { href: "#", class: "link", text: e.summary, onclick: (ev) => { ev.preventDefault(); openEntry(e.id, () => {}); } }), " ",
    h("span", { class: "muted small", text: `${e.author} · ${e.scope}${e.fleet_id ? ` · ${fleetName(e.fleet_id)}` : ""} · written ${fmtAgo(e.created_at)}` }), ...extra));

function withdrawBlock(preview) {
  return h("div", { class: "field" },
    h("label", { text: `Memories to withdraw (${fmtNum(preview.withdraw_total)})` }),
    h("span", { class: "hint", text: "Active memories this agent wrote in the window, in any scope. They are withdrawn with the reason “admin cleanup” and taken off any fleet briefing. An undo puts them back." }),
    preview.withdraw.length
      ? h("div", { class: "modal-scroll cleanup-list" }, h("ul", { class: "merge-list" }, preview.withdraw.map((e) => entryRow(e))))
      : h("span", { class: "muted small", text: "None." }),
    preview.withdraw_truncated
      ? h("span", { class: "hint", text: `Showing the newest ${fmtNum(preview.withdraw.length)}. All ${fmtNum(preview.withdraw_total)} are withdrawn.` })
      : null,
    preview.pins_total ? h("span", { class: "hint", text: `${plural(preview.pins_total, "pin")} on these memories will be removed.` }) : null);
}

function ratingsBlock(preview) {
  const parts = Object.entries(preview.ratings_by_verdict).map(([v, n]) => `${fmtNum(n)} ${v}`);
  return h("div", { class: "field" },
    h("label", { text: `Ratings to set aside (${fmtNum(preview.ratings_total)})` }),
    h("span", { class: "hint", text: "Its ratings last written in the window stop counting towards rankings, flags and metrics. The rows are kept, and rating the same memory again brings one back." }),
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
      h("label", {}, "Memories to bring back: ", count),
      h("span", { class: "hint", text: "Memories this agent replaced with its own memories that are now withdrawn, including through a chain of its own replacements. A ticked one becomes active again. An undo replaces it again." }),
      boxes.length ? h("div", { class: "modal-scroll cleanup-list" }, h("ul", { class: "merge-list" }, items)) : h("span", { class: "muted small", text: "None." }),
      preview.restorable_truncated
        ? h("span", { class: "hint", text: `Showing the newest ${fmtNum(boxes.length)} of ${fmtNum(preview.restorable_total)}; the others stay superseded.` })
        : null),
    chosen: () => boxes.filter((b) => b.box.checked).map((b) => b.e.id),
  };
}

/** Open the clean-up dialog for `agent`. `onDone` runs after a cleanup. */
export function cleanupDialog(agent, onDone) {
  const since = h("input", { type: "datetime-local", id: "cl-since", value: localValue(new Date(Date.now() - 24 * 3600 * 1000)) });
  const until = h("input", { type: "datetime-local", id: "cl-until", title: "Leave empty for now" });
  const previewButton = h("button", { type: "button", class: "btn btn-sm", text: "Preview" });
  const holder = h("div");
  const revoked = agent.status === "revoked";
  const alsoRevoke = h("input", { type: "checkbox", id: "cl-revoke", disabled: revoked });
  const revokeField = h("div", { class: "field" },
    h("label", { class: "check", for: "cl-revoke" }, alsoRevoke, h("span", { text: revoked ? " Already revoked" : " Also revoke this agent" })),
    h("span", { class: "hint", text: revoked
      ? "This agent is already revoked, so only the cleanup runs."
      : "Revokes its key after the cleanup, as its own entry in the audit log. An undo does not un-revoke it." }));
  const confirm = h("input", { id: "cl-confirm", autocomplete: "off", placeholder: agent.name, "aria-label": `Type ${agent.name} to confirm` });
  const confirmField = h("div", { class: "field", hidden: true },
    h("label", { for: "cl-confirm", text: `Type “${agent.name}” to confirm` }), confirm);
  let current = null; // { preview, restore }
  let ticket = 0;

  const bounds = () => {
    if (!since.value) throw new Error("Choose when the window starts.");
    const body = { since: new Date(since.value).toISOString() };
    if (until.value) body.until = new Date(until.value).toISOString();
    return body;
  };
  const run = async () => {
    const mine = ++ticket;
    current = null;
    confirmField.hidden = true;
    let window;
    try { window = bounds(); } catch (err) { holder.replaceChildren(h("p", { class: "form-error", text: err.message })); return; }
    holder.replaceChildren(h("p", { class: "muted", text: `Looking at what ${agent.name} wrote…` }));
    try {
      const preview = await guarded(() => api("POST", `admin/agents/${encodeURIComponent(agent.name)}/cleanup`, { body: { ...window, dry_run: true } }));
      if (mine !== ticket || !preview) return;
      const restore = restoreBlock(preview);
      current = { preview, restore };
      holder.replaceChildren(
        h("p", { class: "small muted", text: `Window: ${fmtTime(preview.since)} to ${fmtTime(preview.until)}. Memories count by when they were written, ratings by when they were last changed.` }),
        ...(preview.nothing_to_clean ? [h("p", { class: "callout small", text: "This agent wrote nothing and rated nothing in this window." })] : []),
        withdrawBlock(preview), ratingsBlock(preview), restore.el);
      confirmField.hidden = false;
    } catch (err) {
      if (mine === ticket) holder.replaceChildren(h("p", { class: "form-error", text: err.message }));
    }
  };
  // A change of the window discards the preview: Clean up needs a fresh one.
  const reset = () => {
    ticket += 1;
    current = null;
    confirmField.hidden = true;
    confirm.value = "";
    holder.replaceChildren();
  };
  since.addEventListener("input", reset);
  until.addEventListener("input", reset);
  previewButton.addEventListener("click", run);

  openModal({
    title: `Clean up after ${agent.name}`,
    wide: true,
    body: [
      h("p", { class: "small muted", text: "For when an agent has written or rated badly: bad memories, a runaway loop. Nothing is deleted, and you can undo it from the list of cleanups." }),
      h("div", { class: "field" },
        h("label", { for: "cl-since", text: "From (local time)" }), since,
        h("label", { for: "cl-until", text: "Until (empty is now)" }), until,
        h("div", { class: "row" }, previewButton)),
      holder, revokeField, confirmField,
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Clean up",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          if (!current) throw new Error("Preview the cleanup first.");
          if (confirm.value.trim() !== agent.name) throw new Error(`Type “${agent.name}” to confirm.`);
          const { preview, restore } = current;
          if (preview.nothing_to_clean && !alsoRevoke.checked) throw new Error("Nothing to clean up in this window.");
          const body = {
            since: preview.since,
            until: preview.until,
            dry_run: false,
            restore_ids: restore.chosen(),
            expected_withdraw: preview.withdraw_total,
            expected_ratings: preview.ratings_total,
            also_revoke: alsoRevoke.checked,
          };
          const done = await guarded(() => api("POST", `admin/agents/${encodeURIComponent(agent.name)}/cleanup`, { body }));
          if (!done) return;
          closeModal();
          const bits = [`${plural(done.withdrawn, "memory", "memories")} withdrawn`, `${plural(done.ratings_set_aside, "rating")} set aside`];
          if (done.restored) bits.push(`${plural(done.restored, "memory", "memories")} brought back`);
          if (done.revoked) bits.push("agent revoked");
          toast(`Cleaned up after ${agent.name}: ${bits.join(", ")}${done.revoke_error ? `. ${done.revoke_error}` : ""}`);
          onDone();
        },
      },
    ],
  });
}

/** Ask before undoing `cleanup` (a row of GET admin/agent-cleanups). */
export function undoCleanupDialog(cleanup, onDone) {
  openModal({
    title: "Undo this cleanup?",
    body: [
      cleanup.self_service ? h("p", { class: "small muted", text: `${cleanup.agent} ran this cleanup itself from the agent portal.` }) : null,
      h("p", {}, `Memories that the cleanup of ${cleanup.agent} withdrew, and that are still withdrawn, become active again. Memories it brought back are replaced again where that still fits, its ratings count again, and pins are restored where the fleet has room.`),
      cleanup.revoked ? h("p", { class: "callout callout-warn small", text: "The cleanup also revoked this agent. Undoing it does not re-activate the agent." }) : null,
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Undo cleanup",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          const done = await guarded(() => api("POST", `admin/agent-cleanups/${encodeURIComponent(cleanup.id)}/undo`));
          if (!done) return;
          closeModal();
          const left = [];
          if (done.reactivate_skipped) left.push(`${plural(done.reactivate_skipped, "memory", "memories")} not re-activated`);
          if (done.resupersede_skipped) left.push(`${plural(done.resupersede_skipped, "restored memory", "restored memories")} not replaced again`);
          if (done.pins_skipped.length) left.push(`${plural(done.pins_skipped.length, "pin")} not restored`);
          toast(`Undone: ${plural(done.reactivated, "memory", "memories")} active again, ${plural(done.ratings_cleared, "rating")} counting again${left.length ? `; ${left.join(", ")} (changed since, or no room)` : ""}${done.note ? `. ${done.note}` : ""}`);
          onDone();
        },
      },
    ],
  });
}

/** The list of past cleanups, newest first, with Undo where it is allowed. */
export function cleanupsCard(cleanups) {
  if (!cleanups) return null;
  const rows = cleanups.map((c) => h("tr", {},
    h("td", { class: "nowrap", title: fmtTime(c.cleaned_at), text: fmtAgo(c.cleaned_at) }),
    h("td", {}, h("strong", { text: c.agent }), c.revoked ? [" ", h("span", { class: "badge badge-revoked", text: "revoked" })] : null),
    h("td", { class: "small", text: `${fmtTime(c.since)} to ${fmtTime(c.until)}` }),
    h("td", { class: "num", text: c.withdrawn }),
    h("td", { class: "num", text: c.ratings_set_aside }),
    h("td", { class: "num", text: c.restored }),
    h("td", {}, c.self_service
      ? [h("span", { class: "badge badge-muted", text: "self-service" }), " ", h("span", { class: "small", text: `run by ${c.cleaned_by} from the agent portal` })]
      : c.cleaned_by),
    h("td", { class: "actions" },
      c.undone_at
        ? h("span", { class: "muted small", title: fmtTime(c.undone_at), text: `undone ${fmtAgo(c.undone_at)}` })
        : (c.undoable
          ? h("button", { type: "button", class: "btn btn-sm", text: "Undo", onclick: () => undoCleanupDialog(c, () => hooks.refresh()) })
          : h("span", { class: "muted small", title: c.undo_blocked || "", text: "undo blocked" })))));
  return card("Cleanups", table(["When", "Agent", "Window", "Withdrawn", "Ratings set aside", "Brought back", "By", ""], rows, "No agent has been cleaned up."), { flush: true });
}
