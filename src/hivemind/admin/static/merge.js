// Hivemind admin panel: merging one fleet into another (ADR 0107), moving some of
// its agents with the memories they wrote (ADR 0112), and undoing either.
// Everything API-sourced is rendered with textContent (see core.js). The dialogs
// always preview first (dry_run) and ask for the source fleet's name before they
// apply. Neither writes a memory or changes a key.

import {
  api, guarded, h, hooks, state, toast, openModal, closeModal, fmtAgo, fmtNum, fmtTime, link, memoriesHref, card, table,
} from "./core.js";
import { openEntry } from "./drawer.js";

const NEW = "__new__";
const ENTRY_STATES = ["active", "superseded", "withdrawn"];

const totalOf = (entries) => Object.values(entries || {}).reduce((a, b) => a + b, 0);

const entryCounts = (entries) => {
  const total = totalOf(entries);
  const parts = ENTRY_STATES.filter((s) => entries && entries[s]).map((s) => `${fmtNum(entries[s])} ${s}`);
  return `${fmtNum(total)} memories${parts.length ? ` (${parts.join(", ")})` : ""}`;
};

// Re-filing a memory rewrites its row and re-inserts its vector into the search
// index: about 12 ms each on a 200k-memory bench (2026-10-29 perf check). Fleet
// writes are refused with 503 fleet_busy until it commits, so say how long.
const MS_PER_MEMORY = 12;
const lockNote = (memories) => {
  const seconds = Math.ceil((memories * MS_PER_MEMORY) / 1000);
  if (seconds < 5) return "Writes to either fleet wait a moment while it runs.";
  const about = seconds < 90 ? `${seconds} seconds` : `${Math.round(seconds / 60)} minutes`;
  return `Re-filing ${fmtNum(memories)} memories can take up to about ${about}, and the undo as long. Until it finishes, writes to either fleet are refused with "fleet busy, retry", so pick a quiet time.`;
};

const side = (s) => h("div", { class: "merge-side" },
  h("h3", { text: s.new ? `${s.name} (new fleet)` : s.name }),
  h("div", { class: "small", text: `${fmtNum(s.agents)} agent${s.agents === 1 ? "" : "s"}` }),
  h("div", { class: "small", text: entryCounts(s.entries) }),
  h("div", { class: "small", text: `${fmtNum(s.pins)} pin${s.pins === 1 ? "" : "s"}` }));

/** The pin checklist: the newest `limit` are ticked and no more than `limit` can be. */
function pinChecklist(preview, pool = "The two fleets hold") {
  const names = new Map([[preview.source.id, preview.source.name]]);
  if (preview.target.id) names.set(preview.target.id, preview.target.name);
  const boxes = preview.pins.map((p) => {
    const box = h("input", { type: "checkbox", value: p.entry_id, checked: p.default_kept });
    return { pin: p, box };
  });
  const count = h("strong");
  const sync = () => {
    const kept = boxes.filter((b) => b.box.checked).length;
    count.textContent = `${kept} of ${preview.pin_limit}`;
    for (const b of boxes) b.box.disabled = !b.box.checked && kept >= preview.pin_limit;
  };
  for (const b of boxes) b.box.addEventListener("change", sync);
  sync();
  const items = boxes.map(({ pin, box }) => h("li", {},
    h("label", { class: "check" }, box,
      h("span", {}, h("strong", { text: pin.summary || "(entry not found)" }), " ",
        h("span", { class: "muted small", text: `${names.get(pin.fleet_id) || pin.fleet_id} · pinned by ${pin.pinned_by} ${fmtAgo(pin.pinned_at)}` })))));
  return {
    el: h("div", { class: "field" },
      h("label", {}, "Pins to keep: ", count),
      h("span", { class: "hint", text: preview.needs_pin_choice
        ? `${pool} ${boxes.length} pins and a fleet keeps at most ${preview.pin_limit}. The newest are ticked. The others are unpinned, and an undo puts them back.`
        : "All pins are kept. Untick one to unpin it; an undo puts it back." }),
      h("div", { class: "modal-scroll" }, h("ul", { class: "merge-list" }, items))),
    chosen: () => boxes.filter((b) => b.box.checked).map((b) => b.pin.entry_id),
    // Every pin the preview showed: the server refuses the merge if the pins changed since.
    shown: () => boxes.map((b) => b.pin.entry_id),
    // A choice is sent only when one is needed or something was unticked, so a pin
    // added after the preview is never dropped for lack of a tick.
    needsChoice: () => preview.needs_pin_choice || boxes.some((b) => !b.box.checked),
  };
}

function duplicatesBlock(preview) {
  if (!preview.target.id) return null;
  const rows = preview.duplicates.map((d) => h("li", {},
    h("span", { class: "sim", text: d.similarity.toFixed(2) }),
    h("span", {},
      h("a", { href: "#", class: "link", text: d.entry_summary || d.entry_id, onclick: (e) => { e.preventDefault(); openEntry(d.entry_id, () => {}); } }),
      " ≈ ",
      h("a", { href: "#", class: "link", text: d.match_summary || d.match_id, onclick: (e) => { e.preventDefault(); openEntry(d.match_id, () => {}); } }))));
  return h("div", { class: "field" },
    h("label", { text: `Likely duplicates (${preview.duplicates.length})` }),
    h("span", { class: "hint", text: "Shown for your information. A merge changes no memory; agents supersede duplicates as they go." }),
    preview.duplicates_truncated
      ? h("span", { class: "hint", text: `Only the newest ${fmtNum(preview.duplicates_looked_up)} memories of the smaller fleet were checked.` })
      : null,
    rows.length ? h("div", { class: "modal-scroll" }, h("ul", { class: "merge-list" }, rows)) : h("span", { class: "muted small", text: "None found." }),
    h("div", { class: "small" }, link(`Browse ${preview.source.name}`, memoriesHref({ fleet: preview.source.id }), { onclick: closeModal }), " · ",
      link(`Browse ${preview.target.name}`, memoriesHref({ fleet: preview.target.id }), { onclick: closeModal })));
}

/** Open the merge dialog for `source`. `onDone` runs after a merge. */
export function mergeDialog(source, onDone) {
  const others = state.fleets.filter((f) => f.id !== source.id && !f.merged_into);
  const select = h("select", { id: "mg-target" },
    others.map((f) => h("option", { value: f.id, text: f.name })),
    h("option", { value: NEW, text: "+ A new fleet…" }));
  const newName = h("input", { id: "mg-new", placeholder: "new fleet name", hidden: true, autocomplete: "off" });
  const previewButton = h("button", { type: "button", class: "btn btn-sm", text: "Preview" });
  const holder = h("div");
  const confirm = h("input", { id: "mg-confirm", autocomplete: "off", placeholder: source.name, "aria-label": `Type ${source.name} to confirm` });
  const confirmField = h("div", { class: "field", hidden: true },
    h("label", { for: "mg-confirm", text: `Type “${source.name}” to confirm` }), confirm);
  let current = null; // { body, checklist }
  let ticket = 0;

  const targetBody = () => {
    if (select.value === NEW) {
      const name = newName.value.trim();
      if (!name) throw new Error("Enter a name for the new fleet.");
      return { new_fleet_name: name };
    }
    if (!select.value) throw new Error("Choose a fleet to merge into.");
    return { target_fleet_id: select.value };
  };
  const run = async () => {
    const mine = ++ticket;
    current = null;
    confirmField.hidden = true;
    let target;
    try { target = targetBody(); } catch (err) { holder.replaceChildren(h("p", { class: "form-error", text: err.message })); return; }
    holder.replaceChildren(h("p", { class: "muted", text: "Looking at both fleets…" }));
    try {
      const preview = await guarded(() => api("POST", `admin/fleets/${encodeURIComponent(source.id)}/merge`, { body: { ...target, dry_run: true } }));
      if (mine !== ticket || !preview) return;
      const checklist = preview.pins.length ? pinChecklist(preview) : null;
      current = { target, checklist };
      holder.replaceChildren(
        h("div", { class: "merge-sides" }, side(preview.source), side(preview.target)),
        h("p", { class: "callout callout-warn small" },
          `${source.name} gives up all its memories and agents to ${preview.target.name}. Agents keep their keys and read the merged pool from their next request. ${lockNote(totalOf(preview.source.entries))} You can undo it from the list of merges.`),
        ...[checklist && checklist.el, duplicatesBlock(preview)].filter(Boolean));
      confirmField.hidden = false;
    } catch (err) {
      if (mine === ticket) holder.replaceChildren(h("p", { class: "form-error", text: err.message }));
    }
  };
  // Any change of target discards the preview: Merge needs a fresh one.
  const reset = () => {
    ticket += 1;
    current = null;
    confirmField.hidden = true;
    confirm.value = "";
    holder.replaceChildren();
  };
  select.addEventListener("change", () => {
    reset();
    newName.hidden = select.value !== NEW;
    if (!newName.hidden) newName.focus();
    else run();
  });
  newName.addEventListener("input", reset);
  previewButton.addEventListener("click", run);
  if (!others.length) { select.value = NEW; newName.hidden = false; }

  openModal({
    title: `Merge ${source.name} into…`,
    wide: true,
    body: [
      h("div", { class: "field" }, h("label", { for: "mg-target", text: "Merge into" }),
        h("div", { class: "row" }, select, newName, previewButton)),
      holder, confirmField,
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Merge fleets",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          if (!current) throw new Error("Preview the merge first.");
          if (confirm.value.trim() !== source.name) throw new Error(`Type “${source.name}” to confirm.`);
          const body = { ...current.target, dry_run: false };
          if (current.checklist) {
            body.expected_pin_ids = current.checklist.shown();
            if (current.checklist.needsChoice()) body.keep_pin_ids = current.checklist.chosen();
          }
          const done = await guarded(() => api("POST", `admin/fleets/${encodeURIComponent(source.id)}/merge`, { body }));
          if (!done) return;
          closeModal();
          toast(`Merged “${source.name}” into “${done.target.name}”: ${fmtNum(done.entries_moved)} memories, ${done.agents_moved.length} agents`);
          onDone();
        },
      },
    ],
  });
}

const plural = (n, one, many = `${one}s`) => `${fmtNum(n)} ${n === 1 ? one : many}`;

/** Open the "Move agents…" dialog for `source`: pick agents of that fleet and a target. */
export function moveDialog(source, onDone) {
  const members = state.agents.filter((a) => a.home_fleet_id === source.id);
  const others = state.fleets.filter((f) => f.id !== source.id && !f.merged_into);
  const select = h("select", { id: "mv-target" },
    others.map((f) => h("option", { value: f.id, text: f.name })),
    h("option", { value: NEW, text: "+ A new fleet…" }));
  const newName = h("input", { id: "mv-new", placeholder: "new fleet name", hidden: true, autocomplete: "off" });
  const previewButton = h("button", { type: "button", class: "btn btn-sm", text: "Preview" });
  const holder = h("div");
  const boxes = members.map((a) => ({ a, box: h("input", { type: "checkbox", value: a.name }) }));
  const agentList = members.length
    ? h("ul", { class: "merge-list" }, boxes.map(({ a, box }) => h("li", {},
      h("label", { class: "check" }, box, h("span", {}, h("strong", { text: a.name }), " ",
        h("span", { class: "muted small", text: a.status === "active" ? `level ${a.trust_level}` : a.status }))))))
    : h("p", { class: "muted small", text: "No agent has this fleet as its home fleet." });
  const confirm = h("input", { id: "mv-confirm", autocomplete: "off", placeholder: source.name, "aria-label": `Type ${source.name} to confirm` });
  const confirmField = h("div", { class: "field", hidden: true },
    h("label", { for: "mv-confirm", text: `Type “${source.name}” to confirm` }), confirm);
  let current = null; // { body, checklist, agents }
  let ticket = 0;

  const chosen = () => boxes.filter((b) => b.box.checked).map((b) => b.a.name);
  const targetBody = () => {
    const agents = chosen();
    if (!agents.length) throw new Error("Tick at least one agent to move.");
    if (select.value === NEW) {
      const name = newName.value.trim();
      if (!name) throw new Error("Enter a name for the new fleet.");
      return { agents, new_fleet_name: name };
    }
    if (!select.value) throw new Error("Choose a fleet to move the agents into.");
    return { agents, target_fleet_id: select.value };
  };
  const reset = () => {
    ticket += 1;
    current = null;
    confirmField.hidden = true;
    confirm.value = "";
    holder.replaceChildren();
  };
  const run = async () => {
    const mine = ++ticket;
    current = null;
    confirmField.hidden = true;
    let body;
    try { body = targetBody(); } catch (err) { holder.replaceChildren(h("p", { class: "form-error", text: err.message })); return; }
    holder.replaceChildren(h("p", { class: "muted", text: "Looking at what would move…" }));
    try {
      const preview = await guarded(() => api("POST", `admin/fleets/${encodeURIComponent(source.id)}/move`, { body: { ...body, dry_run: true } }));
      if (mine !== ticket || !preview) return;
      const checklist = preview.pins.length ? pinChecklist(preview, `${preview.target.name}'s pins and the pins that move with the memories come to`) : null;
      current = { body, checklist };
      const stay = preview.staying_agents;
      holder.replaceChildren(
        h("div", { class: "merge-sides" }, side(preview.source), side(preview.target)),
        h("p", { class: "callout callout-warn small" },
          `${plural(preview.leaving, "memory", "memories")} ${preview.leaving === 1 ? "leaves" : "leave"} ${source.name} with ${preview.agents.join(", ")} (${preview.agents.length === 1 ? "the fleet memories it wrote" : "the fleet memories they wrote"}).`,
          preview.held_back
            ? ` ${plural(preview.held_back, "other memory", "other memories")} of theirs ${preview.held_back === 1 ? "stays" : "stay"}, because the newest version of ${preview.held_back === 1 ? "its" : "their"} chain stays.`
            : "",
          stay.length
            ? ` ${stay.join(", ")} stay${stay.length === 1 ? "s" : ""} in ${source.name} and lose access to ${preview.leaving === 1 ? "the memory that leaves" : `the ${fmtNum(preview.leaving)} memories that leave`} (privileged agents can still read them as foreign entries).`
            : ` No agent stays in ${source.name}.`,
          ` Keys do not change. ${lockNote(preview.leaving)} You can undo it from the list below the fleets.`),
        checklist && checklist.el);
      confirmField.hidden = false;
    } catch (err) {
      if (mine === ticket) holder.replaceChildren(h("p", { class: "form-error", text: err.message }));
    }
  };
  for (const { box } of boxes) box.addEventListener("change", reset);
  select.addEventListener("change", () => {
    reset();
    newName.hidden = select.value !== NEW;
    if (!newName.hidden) newName.focus();
  });
  newName.addEventListener("input", reset);
  previewButton.addEventListener("click", run);
  if (!others.length) { select.value = NEW; newName.hidden = false; }

  openModal({
    title: `Move agents out of ${source.name}`,
    wide: true,
    body: [
      h("div", { class: "field" }, h("label", { text: "Agents to move" }), agentList),
      h("div", { class: "field" }, h("label", { for: "mv-target", text: "Move them into" }),
        h("div", { class: "row" }, select, newName, previewButton)),
      holder, confirmField,
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Move agents",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          if (!current) throw new Error("Preview the move first.");
          if (confirm.value.trim() !== source.name) throw new Error(`Type “${source.name}” to confirm.`);
          const body = { ...current.body, dry_run: false };
          if (current.checklist) {
            body.expected_pin_ids = current.checklist.shown();
            if (current.checklist.needsChoice()) body.keep_pin_ids = current.checklist.chosen();
          }
          const done = await guarded(() => api("POST", `admin/fleets/${encodeURIComponent(source.id)}/move`, { body }));
          if (!done) return;
          closeModal();
          toast(`Moved ${plural(done.agents_moved.length, "agent")} and ${plural(done.entries_moved, "memory", "memories")} to “${done.target.name}”`);
          onDone();
        },
      },
    ],
  });
}

/** Ask before undoing `merge` (a row of GET admin/fleet-merges). */
export function undoDialog(merge, onDone) {
  const move = merge.kind === "move";
  const from = merge.source_fleet_name || merge.source_fleet_id;
  const to = merge.target_fleet_name || merge.target_fleet_id;
  openModal({
    title: move ? "Undo this move?" : "Undo this merge?",
    body: [
      h("p", {}, move
        ? `The agents and memories that “${from}” gave to “${to}” in this move, and that are still there, go back. Memories written since stay where they are, and pins that were dropped are restored where they fit.`
        : `Memories and agents that “${from}” gave to “${to}” and that are still there go back. Memories written since stay where they are, and pins that were dropped are restored where they fit. Search counters stay summed.`),
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: move ? "Undo move" : "Undo merge",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          const done = await guarded(() => api("POST", `admin/fleet-merges/${encodeURIComponent(merge.id)}/undo`));
          if (!done) return;
          closeModal();
          const skipped = done.pins_skipped.length;
          toast(`Undone: ${fmtNum(done.entries_restored)} memories, ${done.agents_restored.length} agents${skipped ? `; ${skipped} pin${skipped === 1 ? "" : "s"} did not fit and stayed unpinned` : ""}`);
          onDone();
        },
      },
    ],
  });
}

/** The list of past merges and moves, newest first, with Undo where it is allowed. */
export function mergesCard(merges) {
  if (!merges) return null;
  const rows = merges.map((m) => h("tr", {},
    h("td", { class: "nowrap", title: fmtTime(m.merged_at), text: fmtAgo(m.merged_at) }),
    h("td", {}, h("span", { class: "badge badge-muted", text: m.kind === "move" ? "move" : "merge" }), " ",
      h("strong", { text: m.source_fleet_name || m.source_fleet_id }), " → ", h("strong", { text: m.target_fleet_name || m.target_fleet_id })),
    h("td", { class: "num", title: m.moved_agents.join(", "), text: m.moved_agents.length }),
    h("td", { class: "num", text: m.dropped_pins }),
    h("td", { text: m.merged_by }),
    h("td", { class: "actions" },
      m.undone_at
        ? h("span", { class: "muted small", title: fmtTime(m.undone_at), text: `undone ${fmtAgo(m.undone_at)}` })
        : (m.undoable
          ? h("button", { type: "button", class: "btn btn-sm", text: "Undo", onclick: () => undoDialog(m, () => hooks.refresh()) })
          : h("span", { class: "muted small", title: m.undo_blocked || "", text: "undo blocked" })))));
  return card("Merges and moves", table(["When", "Fleets", "Agents", "Pins dropped", "By", ""], rows, "No fleets have been merged and no agents moved."), { flush: true });
}
