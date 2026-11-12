# The portal search can explain the ranking too

Amends [ADR 0137](0137-admin-search-explains-the-ranking.md) (the admin search explains the ranking)
and [ADR 0095](0095-agent-portal.md) (the agent portal), and builds on
[ADR 0033](0033-server-enforced-reads-provenance-and-supersession-scope.md) (an entry the key cannot read is not found).

Status: Accepted.

## Context

ADR 0137 kept the score parts to the admin search. The person behind an agent asks the same question
about their own agent ("why did it get this memory first, and not that one?") and today has to ask the
admin. The formula is in SPEC §6.4 and is no secret; the concern is only that the parts must not tell
the person anything about memories the key cannot read.

## Decision

1. **`POST /v1/portal/search` takes `explain` (default false)**, with the same `explain` object on each
   hit as the admin search (`ExplainedHitOut`, renamed from `AdminHitOut`). With it off the response is
   byte for byte what it was. `POST /v1/search` and the MCP tools still have no such field.

2. **Nothing about unreadable memories leaks.** The portal search runs with the key's own visibility, so
   both candidate streams are read with it: the keyword and vector ranks count only memories the key
   can read, and a hit only ever exists for a memory it can read. The other parts (feedback counts,
   importance, age) are the hit's own, and the portal already shows them. A test seeds unreadable
   memories that match better than every readable one and checks that no rank goes past the readable
   count.

3. **The portal Search page has the same "Explain ranking" checkbox**, kept in the hash as `explain=1`.
   The row helper moved into the shared `lists.js` (`withExplanation`), and `explain.js` is now one of
   the shared modules the portal serves ([ADR 0091](0091-admin-panel-shared-front-end-modules.md)).

## Consequences

- An agent's owner can see why its search ranked a memory where it did, over what the agent can read,
  without asking the admin.
- The "Hidden from" card stays admin only: the portal never says what the key cannot see.
