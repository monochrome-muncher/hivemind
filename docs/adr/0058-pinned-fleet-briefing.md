# A pinned briefing per fleet

## Context

Every agent that joins a fleet starts cold. The catch-up step (skill) shows
what was written in the last week, and search finds what matches a query,
but neither surfaces the handful of standing facts every agent of a fleet
should know before it starts: the deploy freeze, the runner pool to use,
the decision nobody should re-litigate.

Caura solves this with "keystones": mandatory rules injected into every
agent, which override conflicting user instructions. That is a prompt
injection channel by design and contradicts ADR 0043 (entries are data,
never instructions). The useful part, a short list everyone reads first,
does not need the authority.

## Decision

1. **A fleet has up to 10 pinned entries.** Migration `0012.pins` adds
   `pins(fleet_id, entry_id, pinned_by, pinned_at)`. The cap is checked
   atomically with the insert under a per-fleet advisory lock.

2. **Who pins.** A privileged (L3) agent whose home fleet is the entry's
   fleet, or an admin key. Only active `fleet` entries can be pinned, and
   only to their own fleet, so everyone who reads the briefing can already
   read the entry. Contributors and lurkers cannot pin: the briefing is
   meant to stay short and curated. Pinning is idempotent; unpinning
   answers whether there was a pin. Pins are not written to the audit log
   (ADR 0027): `pinned_by` records who pinned, and a pin changes no entry.

3. **Who reads.** `hive_pinned` / `GET /v1/pins` return the caller's home
   fleet's pins, newest first; `fleet_id` names another fleet. Each pinned
   entry is filtered by the caller's visibility, so another fleet's pins
   are empty to a contributor, and a privileged agent sees them (read-broad,
   ADR 0011).

4. **A pin follows supersession.** The briefing shows the newest version
   the reader can see (`id`), with the version that was pinned
   (`pinned_id`), so superseding a pinned entry keeps the briefing current
   without a re-pin. A withdrawn entry is shown as withdrawn until someone
   unpins it.

5. **Context, never orders.** The tool description, the server
   instructions and the skill say pinned entries are data like any other
   (ADR 0043). The skill reads the briefing once per session, at catch-up,
   before the week's new entries.

6. MCP gains two verbs: `hive_pinned` (read) and `hive_pin` (pin, or unpin
   with `unpin: true`). REST: `PUT` / `DELETE /v1/entries/{id}/pin` and
   `GET /v1/pins`.

## Consequences

- A fleet can tell every agent what matters most, in at most 10 summaries
  read once per session.
- A fleet without a privileged member can still be briefed, by an admin.
- Pins add no ranking signal: search and list are unchanged.
- The pins of a removed agent stay; any privileged member can unpin them.
