// Hivemind admin panel: offboarding an owner (ADR 0135).
// Everything API-sourced is rendered with textContent (see core.js). The dialog always
// previews first (dry_run) and asks for the owner alias before it applies anything.

import { api, guarded, h, state, toast, openModal, closeModal, fmtNum, table } from "./core.js";

const plural = (n, one, many) => `${fmtNum(n)} ${n === 1 ? one : many}`;

const ACTION_TEXT = {
  retire: (a, successor) => `Retire to ${successor}`,
  revoke: () => "Revoke",
  reject: () => "Reject the registration",
  skip: (a) => `Skip: ${a.reason}`,
};

function passesOver(agent) {
  if (agent.action !== "retire") return "—";
  return [plural(agent.self_notes, "private note", "private notes"), plural(agent.fleet_memories, "fleet memory", "fleet memories"),
    plural(agent.pins, "pin", "pins")].join(", ");
}

function previewBlock(preview) {
  const rows = preview.agents.map((a) => h("tr", {},
    h("td", {}, h("strong", { text: a.name })),
    h("td", { text: a.was }),
    h("td", { text: ACTION_TEXT[a.action](a, preview.successor) }),
    h("td", { text: passesOver(a) })));
  const undo = preview.mode === "retire"
    ? `${preview.successor} will own what these agents wrote (their author stays the agent). Each retirement can be undone on its own from the Agents page; the agent then stays revoked and needs a new key to come back.`
    : "Their keys stop working now and their memories keep no owner. A revoke cannot be undone here: an agent would need to be activated again with a new key.";
  return [
    table(["Agent", "Now", "Offboarding does", "Passes over"], rows, "Nothing to do."),
    h("p", { class: "callout callout-warn small", text: `${undo} It all happens at once or not at all.` }),
  ];
}

/** Open the offboard dialog for `owner` (an owner alias). `onDone` runs after the offboarding. */
export function offboardDialog(owner, onDone) {
  const others = state.agents.filter((a) => a.status === "active" && a.owner_alias !== owner)
    .sort((a, b) => a.name.localeCompare(b.name));
  const mode = (value, text, hint, checked) => h("label", { class: "radio" },
    h("input", { type: "radio", name: "ob-mode", value, checked }), " ", h("strong", { text }), h("span", { class: "hint", text: ` ${hint}` }));
  const retire = mode("retire", "Retire to a successor", "Recommended: their memories keep an owner.", true);
  const revoke = mode("revoke", "Revoke all", "Their keys stop; their memories have no owner.", false);
  const select = h("select", { id: "ob-successor" },
    h("option", { value: "", text: "Choose a successor…" }),
    others.map((a) => h("option", { value: a.name, text: `${a.name}${a.owner_alias ? ` (${a.owner_alias})` : ""}` })));
  const successorField = h("div", { class: "field" }, h("label", { for: "ob-successor", text: "Successor (an active agent of another owner)" }), select);
  const holder = h("div");
  const confirm = h("input", { id: "ob-confirm", autocomplete: "off", placeholder: owner, "aria-label": `Type ${owner} to confirm` });
  const confirmField = h("div", { class: "field", hidden: true },
    h("label", { for: "ob-confirm", text: `Type “${owner}” to confirm` }), confirm);
  let current = null;
  let ticket = 0;
  let notice = null;

  const chosenMode = () => (revoke.querySelector("input").checked ? "revoke" : "retire");
  const request = (dryRun) => ({ mode: chosenMode(), successor: chosenMode() === "retire" ? select.value : null, dry_run: dryRun });

  const preview = async () => {
    const mine = ++ticket;
    current = null;
    successorField.hidden = chosenMode() === "revoke";
    confirmField.hidden = true;
    confirm.value = "";
    if (chosenMode() === "retire" && !select.value) { holder.replaceChildren(); return; }
    holder.replaceChildren(h("p", { class: "muted", text: "Working out what happens to each agent…" }));
    try {
      const body = request(true);
      const result = await guarded(() => api("POST", `admin/owners/${encodeURIComponent(owner)}/offboard`, { body }));
      if (mine !== ticket || !result) return;
      current = { ...body, expected_agents: result.agents.map((a) => ({ name: a.name, action: a.action })) };
      holder.replaceChildren(...(notice ? [h("p", { class: "form-error", text: notice })] : []), ...previewBlock(result));
      notice = null;
      confirmField.hidden = false;
    } catch (err) {
      if (mine === ticket) holder.replaceChildren(h("p", { class: "form-error", text: err.message }));
    }
  };
  for (const input of [retire, revoke]) input.querySelector("input").addEventListener("change", preview);
  select.addEventListener("change", preview);

  openModal({
    title: `Offboard ${owner}…`,
    wide: true,
    body: [
      h("p", { class: "muted", text: "For when this owner leaves: handles every agent registered under this owner alias in one step. An alias is a self-reported label, so check the list before you confirm." }),
      h("div", { class: "field" }, retire, revoke),
      successorField, holder, confirmField,
    ],
    actions: [
      { label: "Cancel", class: "btn-ghost" },
      {
        label: "Offboard owner",
        class: "btn-danger-solid",
        submit: true,
        onClick: async () => {
          if (!current) throw new Error(chosenMode() === "retire" ? "Choose a successor and read the preview first." : "Read the preview first.");
          // The alias exactly as stored: it may have surrounding spaces.
          if (confirm.value !== owner) throw new Error(`Type “${owner}” to confirm.`);
          let done;
          try {
            done = await guarded(() => api("POST", `admin/owners/${encodeURIComponent(owner)}/offboard`, { body: { ...current, dry_run: false } }));
          } catch (err) {
            if (err.code !== "offboard_changed") throw err;
            const changed = "The agents changed since you looked. Read the new preview before confirming.";
            notice = changed;
            await preview();
            throw new Error(changed);
          }
          if (!done) return;
          closeModal();
          const acted = done.agents.filter((a) => a.action !== "skip").length;
          toast(`${owner} offboarded: ${plural(acted, "agent", "agents")} ${done.mode === "retire" ? `retired to ${done.successor}` : "revoked"}`);
          onDone();
        },
      },
    ],
  });
  preview();
}
