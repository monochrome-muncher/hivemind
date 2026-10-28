// Hivemind admin panel: retiring an agent with a successor, and undoing it (ADR 0113).
// Everything API-sourced is rendered with textContent (see core.js). The dialog
// always previews first (dry_run) and asks for the agent's name before it retires.
// Retiring revokes the agent's key; the successor then owns every memory the agent wrote.

import {
  api, guarded, h, hooks, state, toast, openModal, closeModal, fmtAgo, fmtNum, fmtTime, card, table,
} from "./core.js";

const plural = (n, one, many) => `${fmtNum(n)} ${n === 1 ? one : many}`;

function previewBlock(preview) {
  const lines = [
    plural(preview.self_notes, "private note", "private notes"),
    plural(preview.fleet_memories, "fleet memory", "fleet memories"),
    plural(preview.pins, "pin", "pins"),
  ];
  const kids = [
    h("p", {}, h("strong", { text: preview.successor }), " will own ", lines.join(", "), " (active ones; older versions go with them)",
      preview.passes_over_from.length > 1 ? ` from ${preview.passes_over_from.join(", ")}` : "", "."),
    h("p", { class: "callout callout-warn small" },
      `${preview.retired_agent} ${preview.revokes_retired ? "is revoked: its key stops working now" : "is already revoked"}. ${preview.successor} can read, supersede and withdraw these memories, sees them in its portal and cannot rate them. Their author stays ${preview.retired_agent}. Undoing it later takes the memories back but does not bring ${preview.retired_agent} back: it would need a new key, as any revoked agent.`),
  ];
  if (preview.fleet_memories_elsewhere) {
    kids.push(h("p", { class: "muted small", text: `${plural(preview.fleet_memories_elsewhere, "fleet memory sits", "fleet memories sit")} in a fleet other than ${preview.successor}'s home fleet. It can read and withdraw them, and supersede them only as the fleet rules allow (a replacement must be written into that same fleet).` }));
  }
  if (preview.pins) {
    kids.push(h("p", { class: "muted small", text: `Pins stay where they are. In ${preview.successor}'s fleet they count toward its 3 pins, and nothing is unpinned.` }));
  }
  return kids;
}

/** Open the retire dialog for `agent` (an active agent row). `onDone` runs after a retirement. */
export function retireDialog(agent, onDone) {
  const others = state.agents.filter((a) => a.status === "active" && a.name !== agent.name)
    .sort((a, b) => a.name.localeCompare(b.name));
  const select = h("select", { id: "rt-successor" },
    h("option", { value: "", text: "Choose a successor…" }),
    others.map((a) => h("option", { value: a.name, text: a.name })));
  const holder = h("div");
  const confirm = h("input", { id: "rt-confirm", autocomplete: "off", placeholder: agent.name, "aria-label": `Type ${agent.name} to confirm` });
  const confirmField = h("div", { class: "field", hidden: true },
    h("label", { for: "rt-confirm", text: `Type “${agent.name}” to confirm` }), confirm);
  let current = null;
  let ticket = 0;

  const preview = async () => {
    const mine = ++ticket;
    current = null;
    confirmField.hidden = true;
    confirm.value = "";
    if (!select.value) { holder.replaceChildren(); return; }
    holder.replaceChildren(h("p", { class: "muted", text: "Counting what passes over…" }));
    try {
      const result = await guarded(() => api("POST", `admin/agents/${encodeURIComponent(agent.name)}/retire`, { body: { successor: select.value, dry_run: true } }));
      if (mine !== ticket || !result) return;
      current = select.value;
      holder.replaceChildren(...previewBlock(result));
      confirmField.hidden = false;
    } catch (err) {
      if (mine === ticket) holder.replaceChildren(h("p", { class: "form-error", text: err.message }));
    }
  };
  select.addEventListener("change", preview);

  openModal({
    title: `Retire ${agent.name}…`,
    wide: true,
    body: [
      h("p", { class: "muted", text: "Hands everything this agent wrote to another agent and revokes the agent." }),
      h("div", { class: "field" }, h("label", { for: "rt-successor", text: "Successor" }), select),
      holder, confirmField,
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Retire agent",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          if (!current) throw new Error("Choose a successor and read the preview first.");
          if (confirm.value.trim() !== agent.name) throw new Error(`Type “${agent.name}” to confirm.`);
          const done = await guarded(() => api("POST", `admin/agents/${encodeURIComponent(agent.name)}/retire`, { body: { successor: current, dry_run: false } }));
          if (!done) return;
          closeModal();
          toast(`${agent.name} retired: ${done.successor} owns its memories`);
          onDone();
        },
      },
    ],
  });
}

/** Ask before undoing `succession` (a row of GET admin/successions). */
export function undoRetireDialog(succession, onDone) {
  openModal({
    title: "Undo this retirement?",
    body: [
      h("p", {}, `${succession.successor} stops owning the memories ${succession.retired_agent} wrote: it can no longer read its private notes, or supersede or withdraw them, and may rate them again. ${succession.retired_agent} stays revoked; activating it again issues a new key.`),
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Undo retirement",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          const done = await guarded(() => api("POST", `admin/successions/${encodeURIComponent(succession.retired_agent)}/undo`));
          if (!done) return;
          closeModal();
          toast(`Retirement of ${succession.retired_agent} undone`);
          onDone();
        },
      },
    ],
  });
}

/** The standing retirements, newest first, each with Undo. */
export function successionsCard(successions) {
  if (!successions) return null;
  const rows = successions.map((s) => h("tr", {},
    h("td", { class: "nowrap", title: fmtTime(s.at), text: fmtAgo(s.at) }),
    h("td", {}, h("strong", { text: s.retired_agent }), " → ", h("strong", { text: s.successor })),
    h("td", { text: s.by }),
    h("td", { class: "actions" },
      h("button", { type: "button", class: "btn btn-sm", text: "Undo", onclick: () => undoRetireDialog(s, () => hooks.refresh()) }))));
  return card("Retired agents", table(["When", "Retired → successor", "By", ""], rows, "No agent has been retired with a successor."), { flush: true });
}
