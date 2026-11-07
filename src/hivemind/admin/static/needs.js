// Hivemind admin panel: what waits on the operator (ADR 0121). Pure: no DOM.

import { viewHref } from "./ui.js";

// ADR 0121: what waits on the operator, from data the page already loaded. Each
// tile shows only above zero and links to the list that explains it; the
// locked-key tile comes first and is the loud one.
export function needsYouItems(agents, metrics) {
  const locked = agents.filter((a) => a.status === "active" && a.key_locked).length;
  const pending = agents.filter((a) => a.status === "pending").length;
  const idle = agents.filter((a) => a.idle).length;
  const idleDays = (agents[0] && agents[0].idle_after_days) || 30;
  // The Health page's own count (flagged 14+ days ago, memory still active).
  const flags = (metrics && metrics.health && metrics.health.flagged_backlog_14d) || 0;
  return [
    { id: "locked", label: "Keys locked from the portal", count: locked, hint: "A leak was reported", href: viewHref("agents", { activity: "locked" }), urgent: true },
    { id: "pending", label: "Pending agents", count: pending, hint: "Waiting for activation", href: viewHref("agents", { status: "pending" }) },
    { id: "idle", label: "Idle agents", count: idle, hint: `No request for ${idleDays}+ days`, href: viewHref("agents", { activity: "idle" }) },
    { id: "flags", label: "Flagged memories", count: flags, hint: "Flagged over 14 days ago, still active", href: viewHref("health", { flags: "old" }) },
  ].filter((i) => i.count > 0);
}
