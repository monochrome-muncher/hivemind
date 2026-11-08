# Find a memory by id in the admin panel and the agent portal

Builds on [ADR 0080](0080-admin-memory-browser-and-activity.md) (the memory browser), [ADR 0095](0095-agent-portal.md) (the agent portal) and [ADR 0033](0033-server-enforced-reads-provenance-and-supersession-scope.md) (an entry the caller may not read answers like one that does not exist).

Status: Accepted.

## Context

A memory id turns up in an audit row, a log line, an agent's answer or a CSV export. Neither front end could open one: the memory browser and the portal's Search page sent the text to the hybrid search, which ranks summaries and bodies and does not look at ids.

## Decision

1. When the search box holds a whole memory id (a UUID, any case, spaces around it ignored), the front end reads that one memory instead of searching, and shows it as a single row. The other filters do not apply to an id, and the result line says so.
2. The read is the one each drawer already uses. The admin panel calls `GET /v1/admin/entries/{id}`. The portal calls `GET /v1/entries/{id}` with the signed-in agent key, so the key's visibility decides, exactly as it does when the drawer opens a memory.
3. A 404 is an empty result, never an error. The API answers an id the key may not read and an id that names nothing with the same 404, so the portal shows the same "nothing it can read" line for both and never shows that a memory exists.
4. No new route, no new filter on the search or list routes, and nothing new on either proxy allowlist. Part of an id is still searched as text.

## Consequences

- A person can paste an id from anywhere and open the memory, in either front end, in one step.
- The portal shows nothing by id that the agent's own `hive_get` would not show.
- Looking up an id is not a search, so it never counts in the search numbers; the portal's read is the same data-plane read the drawer has made since ADR 0095.
