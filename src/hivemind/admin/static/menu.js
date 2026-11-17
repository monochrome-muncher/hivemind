// Hivemind admin panel: a "More" dropdown of menu items.
// A popover lives in the top layer, so a table's overflow never clips it, and
// the browser closes it on Escape and on a click outside; this adds focus,
// arrow keys and the position under the trigger.

import { h } from "./ui.js";

let nextId = 0;

const GAP = 4;

/** `items`: { label, onClick, danger?, title? }. Returns the trigger with its menu. */
export function menu(label, items) {
  const id = `menu-${nextId++}`;
  const list = h("div", { id, class: "menu", role: "menu", popover: "auto" });
  const trigger = h("button", {
    type: "button", class: "btn btn-sm", text: `${label} ▾`, popovertarget: id, "aria-haspopup": "menu", "aria-expanded": "false",
  });
  const entries = items.map((item) => h("button", {
    type: "button", role: "menuitem", class: item.danger ? "menu-item menu-item-danger" : "menu-item", text: item.label, title: item.title,
    onclick: () => { list.hidePopover(); item.onClick(); },
  }));
  list.append(...entries);

  const place = () => {
    const anchor = trigger.getBoundingClientRect();
    const height = list.offsetHeight;
    const below = anchor.bottom + GAP + height <= window.innerHeight || anchor.top < height + GAP;
    list.style.top = `${below ? anchor.bottom + GAP : anchor.top - GAP - height}px`;
    list.style.right = `${document.documentElement.clientWidth - anchor.right}px`;
  };
  const close = () => list.hidePopover();
  const move = (from, step) => entries[(from + step + entries.length) % entries.length].focus();

  list.addEventListener("toggle", (event) => {
    const open = event.newState === "open";
    trigger.setAttribute("aria-expanded", String(open));
    if (open) { place(); entries[0].focus(); window.addEventListener("resize", close); }
    else window.removeEventListener("resize", close);
  });
  list.addEventListener("keydown", (event) => {
    const at = entries.indexOf(document.activeElement);
    if (event.key === "ArrowDown") move(at, 1);
    else if (event.key === "ArrowUp") move(at, -1);
    else if (event.key === "Home") entries[0].focus();
    else if (event.key === "End") entries[entries.length - 1].focus();
    else return;
    event.preventDefault();
  });
  list.addEventListener("focusout", (event) => {
    if (list.matches(":popover-open") && event.relatedTarget && !list.contains(event.relatedTarget)) list.hidePopover();
  });
  return h("span", { class: "menu-wrap" }, trigger, list);
}
