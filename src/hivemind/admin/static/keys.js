// Hivemind admin panel: the org key (ADR 0029).
// Everything API-sourced is rendered with textContent (see core.js).

import {
  api, guarded, h, toast, showKeyOnce, pageHead, fill,
} from "./core.js";

export async function viewKeys(root) {
  const confirmInput = h("input", { placeholder: "type rotate to confirm", autocomplete: "off" });
  const rotate = h("button", { type: "submit", class: "btn btn-danger-solid", text: "Rotate the org key", disabled: true });
  confirmInput.addEventListener("input", () => { rotate.disabled = confirmInput.value.trim() !== "rotate"; });
  const form = h("form", { class: "row" }, confirmInput, rotate);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (confirmInput.value.trim() !== "rotate") return;
    rotate.disabled = true;
    try {
      const result = await guarded(() => api("POST", "admin/org-key/rotate"));
      if (result) {
        confirmInput.value = "";
        showKeyOnce("New org key", result.key, "Every previous org key has stopped working. Redistribute this one to everyone who registers agents.");
      }
    } catch (err) {
      toast(err.message, true);
    }
  });
  fill(root, 
    pageHead("Org key", "The single key shared by the whole cluster. It gates registration only."),
    h("div", { class: "card card-danger" },
      h("div", { class: "card-head" }, h("h2", { text: "Rotate the org key" })),
      h("div", { class: "card-body" },
        h("p", { class: "callout callout-danger", text: "Every existing org key stops working immediately, so nobody can register new agents with an old copy. Active agents are not affected: they use their own agent keys. To cut an agent off, revoke it." }),
        form)),
    h("div", { class: "card" },
      h("div", { class: "card-head" }, h("h2", { text: "Admin keys" })),
      h("div", { class: "card-body" },
        h("p", { class: "muted" }, "Admin keys are issued and revoked with the ", h("code", { text: "hivemind-keys" }),
          " CLI only (", h("code", { text: "issue-admin" }), " / ", h("code", { text: "revoke-admin" }),
          "), so a leaked admin key cannot mint more of itself."))));
}
