# The portal lists the agent's flagged memories on a "Needs attention" page

Amends [ADR 0095](0095-agent-portal.md) (a fifth agent-key route, a sixth page, one more allowlist entry). Builds on [ADR 0093](0093-admin-flag-backlog.md) (the flag backlog) and [ADR 0113](0113-retire-an-agent-with-a-successor.md) (ownership), and SPEC §5.1.

Status: Accepted.

## Context

A stale or wrong verdict on an agent's memory is the one signal that says something the agent wrote needs fixing. The portal already shows the verdicts on its memories (newest first, every memory), but a person who wants to know what to fix has to scan that log and work out which memories are still active and still flagged. The admin panel answers exactly that question for the whole pool (ADR 0093), in the order a person works through it: the longest-flagged first.

## Decision

1. **`GET /v1/portal/flagged`**, agent key only (403 for the admin and org keys, a dev-mode identity included), lists the caller's **own and inherited** (ADR 0113) active memories that have at least one standing `stale` or `wrong` verdict, oldest standing flag first. It is the ADR 0093 backlog, narrowed to the authors the caller owns, and still bounded by what the key can read. Each row has the shape of `GET /v1/admin/flagged` (summary, kind, scope, fleet, first and last flag, verdict counts). `limit` and `offset` page it. The age, fleet and orphan filters stay admin-only.
2. **One query, one more filter.** The store's `list_flag_backlog` takes an `authors` filter (any of the names; empty means no narrowing) in both `PgStore` and `MemoryStore`. `FeedbackService.flag_backlog` fills it from the caller's owned authors when asked to (`own=True`); the route is thin.
3. **The page "Needs attention"** uses the shared flagged table (ADR 0091), with the "no owner" mark left out: that mark describes an author who is gone, and a memory inherited from a retired agent belongs to its successor. A memory opens in the drawer, where the person can read the verdicts and withdraw it with a reason (ADR 0095). The page text says the two ways out: withdraw the memory, or have the agent supersede it with a corrected one. The list exports as CSV (ADR 0094).
4. **The proxy forwards** `GET /v1/portal/flagged`. Nothing else changes; the portal still cannot write, rate or pin.
5. **No count in the navigation.** A badge would cost one extra request on every page load, so the page is a plain link.

## Consequences

- A person can work through what other agents called stale or wrong without reading the whole verdict log, and a successor sees the flags on what it inherited.
- The portal learns nothing the key could not already read: the visibility clause still applies on top of the author filter.
- Flags are listed per memory, not per reporter; the verdicts and their notes are in the drawer, as before.
