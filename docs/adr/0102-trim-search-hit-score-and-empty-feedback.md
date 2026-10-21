# Search hits round `score` and omit all-zero `feedback`

Amends [ADR 0051](0051-feedback-is-readable.md) (a hit carries its feedback counts) and the compact hit of SPEC §6.1. Adds no setting and no field.

Status: Accepted. Display only, on both surfaces.

## Context

A search reply is read by an agent once per query, so every byte of a hit costs context on every search. Two parts of the hit carry no information an agent can use:

- `score` was the raw float, for example `0.01600102405288303`. The skill tells agents to read it as findability, not truth, and nothing client-side sorts or thresholds on it (the admin panel only shows it in a CSV export). Four significant figures separate neighbouring ranks as well as the raw value does for a reader.
- `feedback` was sent on every hit, usually as `{"helpful":0,"stale":0,"wrong":0}`. Most entries have no reports, and SPEC §6.1 already leaves out the other evidence fields when they are empty.

## Decision

1. **`score` is rounded to 4 significant figures** when a hit is serialised, over MCP and REST alike (`wire.round_score`). The rounding is the last step: the service ranks and paginates on the unrounded value, so order, ties and the explore re-order are unchanged. Two hits that differ only past the fourth figure may show the same score; their order is still the ranking order.
2. **`feedback` is left out of a hit when `helpful`, `stale` and `wrong` are all zero**: omitted over MCP, `null` over REST, like the other evidence fields. A reader treats a missing `feedback` as zero counts. When any count is non-zero the object is sent whole, with all three keys.
3. **Only search hits change.** `hive_get`, `hive_list` (relied-on-changed), `related` on a write and the feedback summary keep sending their counts.
4. **Consumers.** The admin panel and the agent portal already read `hit.feedback` as optional (`normaliseEntry` in `lists.js`), so they need no change. A client that reads `feedback.stale` on a hit must test for it first.

## Consequences

A hit is about 44 to 58 bytes smaller on the wire (measured on a ten-hit reply with a typical summary, two tags and a fleet id: 57.7 bytes per hit when no hit has reports, 44.0 when three in ten do). No ranking, filter or stored value changes, so no migration, setting or re-measure. The tool description tells agents that an absent `feedback` means no reports.
