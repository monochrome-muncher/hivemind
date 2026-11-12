// Hivemind front-end kit: "since you last looked". The last visit is kept in the browser
// (localStorage) and the visit's own `since` for the length of the tab (sessionStorage, else
// memory), so Refresh keeps showing what changed since the previous visit. Pure apart from
// the storage objects handed in; every storage call is wrapped, so a blocked store falls back.

export const FALLBACK_DAYS = 7;
const DAY_MS = 24 * 3600 * 1000;
const visits = new Map(); // the in-memory fallback for a blocked sessionStorage

const validTime = (iso) => typeof iso === "string" && iso !== "" && !Number.isNaN(new Date(iso).getTime());

/** The moment to count from: `stored` when it is a past time, else seven days before `now` (`fallback: true`). */
export function resolveSince(stored, now = new Date()) {
  if (validTime(stored) && new Date(stored).getTime() <= now.getTime()) return { since: new Date(stored).toISOString(), fallback: false };
  return { since: new Date(now.getTime() - FALLBACK_DAYS * DAY_MS).toISOString(), fallback: true };
}

const attempt = (fn) => { try { return fn(); } catch { return null; } };

/**
 * The `since` of this visit: the visit already begun in this tab, else the stored last visit.
 * `commit()` records now as the last visit and pins this `since` for the tab; call it once the
 * view has loaded. A visit that never loaded stores nothing.
 */
export function beginVisit(key, { now = new Date(), local = null, session = null } = {}) {
  const pinned = attempt(() => JSON.parse(session.getItem(`${key}.visit`)))
    || visits.get(key) || null;
  if (pinned && validTime(pinned.since)) return { since: pinned.since, fallback: Boolean(pinned.fallback), commit: () => {} };
  const resolved = resolveSince(attempt(() => local.getItem(key)), now);
  return {
    ...resolved,
    commit: () => {
      visits.set(key, resolved);
      attempt(() => session.setItem(`${key}.visit`, JSON.stringify(resolved)));
      attempt(() => local.setItem(key, now.toISOString()));
    },
  };
}

/** Forget the in-memory visits (for tests). */
export const resetVisits = () => visits.clear();

/** "Tue 11 Nov, 14:02", in the viewer's time zone unless `timeZone` says otherwise. */
export function sinceLabel(iso, timeZone = undefined) {
  const d = new Date(iso);
  const part = (opts) => new Intl.DateTimeFormat("en-GB", { timeZone, ...opts }).format(d);
  return `${part({ weekday: "short" })} ${part({ day: "numeric" })} ${part({ month: "short" })}, ${part({ hour: "2-digit", minute: "2-digit", hourCycle: "h23" })}`;
}

/** "since Tue 11 Nov, 14:02", or "in the last 7 days" when there was no usable last visit. */
export const sincePhrase = ({ since, fallback }, timeZone = undefined) =>
  (fallback ? `in the last ${FALLBACK_DAYS} days` : `since ${sinceLabel(since, timeZone)}`);

/** A count for display: "100+" when the page that was counted was full. */
export const countText = (n, limit, full) => (full || n >= limit ? `${n >= limit ? limit : n}+` : String(n));

/** How many of `items` carry a `field` time at or after `since`. */
export const countSince = (items, field, since) => {
  const from = new Date(since).getTime();
  return items.filter((x) => validTime(x[field]) && new Date(x[field]).getTime() >= from).length;
};

const noun = (n, one, many) => (n === 1 ? one : many);

/** Audit actions the counts above already cover (the agents list, the locked keys): not "other admin actions". */
export const COUNTED_ELSEWHERE = ["agent.register", "agent.activate", "agent_key.lock"];

/** An admin action the "other admin actions" part counts: not a withdrawal, not counted above, not done with an agent key. */
const isOtherAdminAction = (r) => r.action !== "entry.withdraw" && !COUNTED_ELSEWHERE.includes(r.action) && r.actor_kind !== "agent_key";

// The flagged list is oldest flag first and the API pages by 100, so the Overview reads up to three pages.
export const FLAGGED_PAGE = 100;
export const ADMIN_LIMITS = { entries: 100, flagged: 3 * FLAGGED_PAGE, audit: 200 };

/**
 * The parts of the Overview line. A read that failed is `null`/`undefined` and leaves its part out;
 * a part at zero is left out too. `agents` is the AdminAgentOut list, `entries`, `flagged` and
 * `audit` the rows the reads returned. Each part: `{ id, text }`.
 */
export function adminParts({ since, agents = null, entries = null, flagged = null, audit = null }) {
  const out = [];
  const add = (id, shown, one, many, count) => { if (count > 0) out.push({ id, text: `${shown} ${noun(count, one, many)}` }); };
  if (entries) {
    const n = entries.length;
    add("memories", countText(n, ADMIN_LIMITS.entries), "new memory", "new memories", n);
  }
  if (agents) {
    const created = countSince(agents, "created_at", since);
    add("agents", String(created), "new agent", "new agents", created);
    const locked = countSince(agents, "key_locked_at", since);
    add("locked", String(locked), "key locked", "keys locked", locked);
  }
  if (flagged) {
    const n = countSince(flagged, "first_flag_at", since);
    add("flags", countText(n, ADMIN_LIMITS.flagged, flagged.length >= ADMIN_LIMITS.flagged), "new flag", "new flags", n);
  }
  if (audit) {
    const full = audit.length >= ADMIN_LIMITS.audit;
    const withdrawals = audit.filter((r) => r.action === "entry.withdraw").length;
    const other = audit.filter(isOtherAdminAction).length;
    add("withdrawals", countText(withdrawals, ADMIN_LIMITS.audit, full), "withdrawal", "withdrawals", withdrawals);
    add("actions", countText(other, ADMIN_LIMITS.audit, full), "other admin action", "other admin actions", other);
  }
  return out;
}

/** The portal's parts: new memories and new pins in the home fleet. `memoriesCount` is the count (or null when unread), `pins` the pin list. */
export function portalParts({ since, memories = null, memoriesFull = false, pins = null }) {
  const out = [];
  if (memories !== null && memories > 0) {
    out.push({ id: "memories", text: `${countText(memories, PORTAL_LIMIT, memoriesFull)} ${noun(memories, "new memory", "new memories")}` });
  }
  if (pins) {
    const n = countSince(pins, "pinned_at", since);
    if (n > 0) out.push({ id: "pins", text: `${n} ${noun(n, "new pin", "new pins")}` });
  }
  return out;
}
export const PORTAL_LIMIT = 100;

/** The sentence's head: "Since Mon 10 Nov" / "In the last 7 days", and "Nothing new since …" when no part is left. */
export function sinceHead(visit, parts, timeZone = undefined) {
  const phrase = sincePhrase(visit, timeZone);
  return parts.length ? `${phrase[0].toUpperCase()}${phrase.slice(1)}: ` : `Nothing new ${phrase}.`;
}
