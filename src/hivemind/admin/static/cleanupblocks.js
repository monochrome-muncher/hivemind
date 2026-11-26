// Hivemind admin panel and portal: the preview blocks of the cleanup dialog (ADRs 0111, 0118).
// Pure view code, shared as it is (ADR 0091): what the two UIs say differently comes in as
// `words`, and the fleet's display name as `fleetName`. Everything API-sourced is rendered with
// textContent (h()).

import { fmtAgo, fmtNum, h, kindBadge, plural } from "./ui.js";
import { openEntry } from "./drawer.js";

/**
 * The preview blocks of one UI. `words` holds its texts: `withdrawHint`, `ratingsLabel`,
 * `ratingsHint`, `restoreLabel` and `restoreHint`.
 */
export function cleanupBlocks({ fleetName, words }) {
  const entryRow = (e) => h("li", {},
    kindBadge(e.kind),
    h("span", {},
      h("a", { href: "#", class: "link", text: e.summary, onclick: (ev) => { ev.preventDefault(); openEntry(e.id, () => {}); } }), " ",
      h("span", { class: "muted small", text: `${e.author} · ${e.scope}${e.fleet_id ? ` · ${fleetName(e.fleet_id)}` : ""} · written ${fmtAgo(e.created_at)}` })));

  function withdrawBlock(preview) {
    return h("div", { class: "field" },
      h("label", { text: `Memories to withdraw (${fmtNum(preview.withdraw_total)})` }),
      h("span", { class: "hint", text: words.withdrawHint }),
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
      h("label", { text: `${words.ratingsLabel} (${fmtNum(preview.ratings_total)})` }),
      h("span", { class: "hint", text: words.ratingsHint }),
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
        h("label", {}, `${words.restoreLabel}: `, count),
        h("span", { class: "hint", text: words.restoreHint }),
        boxes.length ? h("div", { class: "modal-scroll cleanup-list" }, h("ul", { class: "merge-list" }, items)) : h("span", { class: "muted small", text: "None." }),
        preview.restorable_truncated
          ? h("span", { class: "hint", text: `Showing the newest ${fmtNum(boxes.length)} of ${fmtNum(preview.restorable_total)}; the others stay superseded.` })
          : null),
      chosen: () => boxes.filter((b) => b.box.checked).map((b) => b.e.id),
    };
  }

  return { withdrawBlock, ratingsBlock, restoreBlock };
}
