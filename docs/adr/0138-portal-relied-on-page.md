# The portal lists what the agent relied on that has since changed

Amends [ADR 0095](0095-agent-portal.md) (a new agent-key route, a new page, one more allowlist entry). Builds on [ADR 0098](0098-relied-on-changed-list-filter.md) (the `relied_on_changed` filter), [ADR 0113](0113-retire-an-agent-with-a-successor.md) (successions) and [ADR 0033](0033-server-enforced-reads-provenance-and-supersession-scope.md) (an unreadable entry is not found), and SPEC §5.1.

Status: Accepted.

## Context

An agent that rates a memory `helpful` is saying its work used it. ADR 0098 lets the agent list those memories once they are superseded, withdrawn or flagged by someone else, but only through `hive_list` and `GET /v1/entries`, which the person behind the agent cannot reach. The portal shows what the agent wrote and what was said about it, never what the agent leaned on that has since moved. The agent may be building on something out of date, and the person has no way to see it.

## Decision

1. **`GET /v1/portal/relied-on`**, agent key only (403 for the admin and org keys, a dev-mode identity included), lists the same rows `GET /v1/entries?relied_on_changed=true` gives that key: same visibility, same reporter identity, same order. It takes `limit` and `offset` like `GET /v1/portal/entries`, and an optional `kind`. Nothing about the filter changes.
2. **Whose ratings count.** The filter keys the caller as feedback rows are keyed: the key's user and agent (ADR 0098, item 4). A successor's inherited agents (ADR 0113) are not folded in. Their ratings were filed under their own names, so the list shows what this agent itself rated. This is how the filter already behaves, and the portal follows it.
3. **Order.** Newest memory first, the filter's own order. "Most recently changed first" would mean sorting before paging on a change date the filter does not hold, which ADR 0098 (item 6) declined for the same reason as ADR 0054.
4. **Each row says what changed.** The response model `PortalReliedOnOut` is the entry as `EntryOut` has it, with its verdict counts (as `GET /v1/portal/entries`), plus a `change` object: `rated_helpful_at`; `superseded` and `superseded_by`; `withdrawn` and `withdrawn_at`; `flagged_since` (`{stale, wrong}`, the reports other users filed after the rating, or null). Only changes that qualify under ADR 0098 are reported, so an entry superseded by the caller's own user shows no supersession. "Someone else" means another owner (user), not another agent under the same user. A listed entry whose change facts are missing (it moved between the two reads) is still listed, with nothing true and a null `rated_helpful_at`, and the page says "Changed since it was rated".
5. **A successor id only if readable.** `superseded_by` is set only when the key can read the successor; otherwise `superseded` is true and the id is null, as everywhere else (ADR 0033). The withdrawal reason is the entry's existing `withdrawn_reason`, which `EntryOut` already gives a reader of the entry. Nothing more is exposed.
6. **One batched read for the change.** The `Store` port gains `relied_on_changes(entry_ids, reporter)`, which returns, for the listed entries the reporter rated helpful, the facts above (a `ReliedOnChange`) in one query on `PgStore` and one pass on `MemoryStore`. `ReadService.relied_on_changed` lists through the existing `list`, then asks for the changes; the route only serializes. A shared contract test runs both stores. No migration, no index.
7. **The page "Relied on"**, between "Needs attention" and "Verdicts it gave", at `#/relied`. Each row shows the memory (it opens the drawer), kind, author, the change in words ("Superseded by a newer version" with a button to open the successor when its id is given; "Withdrawn <when>: <reason>"; "Rated stale 2, wrong 1 since"), when the agent rated it helpful, and the verdict counts. The intro says what the list is and what to do: read the change, tell the agent to re-check, or have it rate the memory again. It exports as CSV. The proxy forwards `GET /v1/portal/relied-on`.

## Consequences

- A person can see which memories their agent built on have since been replaced, withdrawn or called stale or wrong, without the agent's key and a `curl`.
- An item leaves the list the way ADR 0098 says: the agent re-checks and rates it again after the change.
- The portal learns nothing the key could not already read through `GET /v1/entries`; a successor the key cannot read stays unnamed.
- A successor does not see what its retired predecessors relied on. If that proves wanted, it is a change to the filter's reporter, decided in its own ADR.
- Paging is by memory age, so a recently changed old memory can sit on a later page.
