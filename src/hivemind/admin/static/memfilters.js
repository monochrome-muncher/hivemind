// Hivemind front-end kit: the memory filters both the admin panel and the agent portal offer.
//
// The state choice, tags, entity and the two date ranges: the form fields (with the less
// common ones folded into a "More filters" row), their hash form, and the API fields they
// become. Knows no route and no key. Dates are whole UTC days.

import { h } from "./ui.js";

export const STATES = [["active", "Active"], ["withdrawn", "Withdrawn"], ["superseded", "Superseded"], ["all", "All"]];
const STATE_VALUES = STATES.map(([value]) => value);
const MORE = ["tags", "entity", "created_from", "created_to", "occurred_from", "occurred_to"];

/** The names in a comma-separated field: trimmed, empties dropped. */
export const parseNames = (text) => String(text || "").split(",").map((s) => s.trim()).filter(Boolean);

/** `names` plus `name`, once (case-insensitive), as a comma-separated field. */
export const addName = (text, name) => {
  const names = parseNames(text);
  if (!names.some((n) => n.toLowerCase() === name.toLowerCase())) names.push(name);
  return names.join(", ");
};

/** The first and the last moment of the UTC day `day` (YYYY-MM-DD); the API's range bounds are inclusive. */
export const dayStart = (day) => (day ? `${day}T00:00:00Z` : "");
export const dayEnd = (day) => (day ? `${day}T23:59:59.999999Z` : "");

/** The state in a hash: `state`, else the old `all=1` (All) or `active=1` (Active), else `fallback`. */
export function stateFromHash(get, fallback) {
  const named = get("state");
  if (STATE_VALUES.includes(named)) return named;
  if (get("all") === "1") return "all";
  if (get("active") === "1") return "active";
  return fallback;
}

/** The API fields for filter values `f` (from `read()` below): only what is set. */
export function filterFields(f) {
  const out = f.state === "all" ? { include_inactive: true } : { state: f.state };
  const tags = parseNames(f.tags);
  const entities = parseNames(f.entity);
  if (tags.length) out.tags = tags;
  if (entities.length) out.entities = entities;
  if (f.created_from) out.created_from = dayStart(f.created_from);
  if (f.created_to) out.created_to = dayEnd(f.created_to);
  if (f.occurred_from) out.occurred_from = dayStart(f.occurred_from);
  if (f.occurred_to) out.occurred_to = dayEnd(f.occurred_to);
  return out;
}

const textField = (label, value, placeholder) => h("input", { type: "text", value, placeholder, "aria-label": label });
const dayField = (label, value) => h("label", { class: "day-field small muted" }, `${label} `, h("input", { type: "date", value, "aria-label": label }));

/**
 * The filter fields. `get(key)` reads the hash, `fallback` is the state shown when it names
 * none. `state` goes in the main filter row, `more` (a <details>, open when any of its fields
 * is set) after it. `read()` gives the values as hash-ready strings, `clear()` empties them,
 * and `setTag` / `setEntity` add one name to the field and open the row.
 */
export function memoryFilterFields(get, fallback) {
  const state = h("select", { "aria-label": "State" },
    STATES.map(([value, text]) => h("option", { value, text, selected: value === stateFromHash(get, fallback) })));
  const tags = textField("Tags, comma-separated, all must match", get("tags"), "Tags (comma-separated)");
  const entity = textField("Entities, comma-separated, all must match", get("entity"), "Entity");
  const dates = {
    created_from: dayField("Written from", get("created_from")),
    created_to: dayField("to", get("created_to")),
    occurred_from: dayField("Memory date from", get("occurred_from")),
    occurred_to: dayField("to", get("occurred_to")),
  };
  const input = (label) => label.querySelector("input");
  const more = h("details", { class: "more-filters" },
    h("summary", { text: "More filters" }),
    h("div", { class: "filters" }, tags, entity,
      h("span", { class: "day-range" }, dates.created_from, dates.created_to),
      h("span", { class: "day-range" }, dates.occurred_from, dates.occurred_to)));
  more.open = MORE.some((key) => get(key));
  const field = { tags, entity, ...Object.fromEntries(Object.entries(dates).map(([k, label]) => [k, input(label)])) };

  return {
    state,
    more,
    read: () => ({ state: state.value, ...Object.fromEntries(MORE.map((key) => [key, field[key].value.trim()])) }),
    clear: () => {
      state.value = fallback;
      for (const key of MORE) field[key].value = "";
    },
    setTag: (name) => { field.tags.value = addName(field.tags.value, name); more.open = true; },
    setEntity: (name) => { field.entity.value = addName(field.entity.value, name); more.open = true; },
    changeTargets: () => [state, ...MORE.map((key) => field[key])],
  };
}
