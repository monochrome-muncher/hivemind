# The memory browsers filter by exact state, tags, entity and dates

Amends [ADR 0080](0080-admin-memory-browser-and-activity.md) (the admin memory browser)
and [ADR 0095](0095-agent-portal.md) (the agent portal), and builds on
[ADR 0011](0011-fleets-and-trust-levels.md) (visibility) and
[ADR 0040](0040-input-bounds-and-agent-name-rules.md) (input bounds).

Status: Accepted.

## Context

The memory browsers had one state choice, "Active only" or "Include inactive". Someone looking for what
was withdrawn had to list everything and scan for the badge. Tags, entities and the two date ranges were
already filters of the API, but the admin panel offered none of them, and the portal list took neither
entities nor dates.

## Decision

1. **`EntryFilters` gets an exact `state`.** When set, only entries in that state match, whatever
   `include_inactive` says. One method on `EntryFilters` (`required_state`) says which state a filter
   admits; `matches`, the Postgres conditions and the search post-filter all read it. Hits carry their
   `state` whenever the filter can admit an inactive entry.

2. **`state` is a browser filter, not a data-plane one.** It is accepted on `GET /v1/admin/entries`,
   `POST /v1/admin/search`, `GET /v1/portal/entries` and `POST /v1/portal/search`. `GET /v1/entries`,
   `POST /v1/search` and the MCP tools are unchanged: an agent still sees inactive entries only through
   `include_inactive`. `tests/unit/test_filter_parity.py` pins which surface takes which filter.

3. **`GET /v1/portal/entries` takes `entities`, `occurred_from`, `occurred_to`, `created_from` and
   `created_to`**, as the portal search body already did. No read right is added: the key's visibility
   still bounds every row, so no filter shows another fleet's or another agent's `self` memories.

4. **The pages.** Both browsers replace the state select with Active, Withdrawn, Superseded and All, and
   add Tags (comma-separated, all must match), Entity, a "Written" range and a "Memory date" range under
   a "More filters" row that opens by itself when one is set. The admin panel keeps `all=1` in old links
   as All. Dates are whole UTC days: the end of a range is sent as the last microsecond of its day, since
   the API's bounds are inclusive, so a one-day range works. Tag chips in a row and tag and entity chips
   in the drawer are buttons that set the filter. The portal's "Its memories" still defaults to every
   state; the portal Search page defaults to Active.

## Consequences

- Finding what was withdrawn or superseded is one choice, and a tag or entity is one click away.
- A shared front-end module, `memfilters.js`, holds the form and its API fields for both pages.
- The home fleet list of the portal has no filter form and stays as it was.
