# A `relied_on_changed` filter for entries an agent rated helpful that have since changed

> **Status:** Accepted.

## Context

An agent that rates an entry `helpful` is telling the pool it built on it.
If that entry is later superseded, withdrawn, or reported `stale` or
`wrong` by someone else, nothing tells the agent. Three signals exist and
none covers this:

- `upstream_changed` (ADR 0081) marks an insight or decision whose
  `see_also` target changed. It reads links, not ratings.
- `changed_since_pin` (ADR 0076) covers pins only.
- The skill's catch-up step lists the agent's own flagged entries
  (ADR 0054), which are entries the agent wrote, not entries it relied on.

The result is the retraction problem: a correction exists in the pool but
does not reach the readers who already acted on the old claim. Citation
studies find that retracted papers keep being cited as if sound (Budd et
al. 1998), and work on shared agent memory names the same gap and calls
for "stale propagation" to dependents (Governed Shared Memory for
Multi-Agent LLM Systems, arXiv 2606.24535). Truth maintenance systems
(Doyle 1979) record what a belief rests on so that a retraction can find
its dependents. A `helpful` rating is the dependency record Hivemind
already has: it says "my work used this and it held".

## Decision

1. **`EntryFilters` gains `relied_on_changed`**, a public filter on
   `hive_list` and `GET /v1/entries` (`?relied_on_changed=true`). It is not
   added to search: the point is a short list to read at catch-up, not a
   ranked answer.

2. **An entry matches when the caller has a `helpful` feedback row on it
   and, since that row's `updated_at`, at least one of:**
   - the entry was withdrawn (its `withdrawn_at` is later; see item 9);
   - the entry was superseded by someone else, and the successor was
     created after the row (a rating made after the entry was already
     replaced is not news);
   - another reporter filed `stale` or `wrong` with a later `updated_at`,
     and that reporter is not revoked (ADR 0067).

   "Someone else" is judged by user: a successor written by the caller's
   own user, or a `stale` or `wrong` row from another agent instance under
   the same user, is the caller's own change and does not count. The caller
   has one verdict per entry (one row per entry, user and agent), so its
   own later `stale` or `wrong` replaces the `helpful` row and the entry
   drops out.

3. **The filter implies inactive entries.** A superseded or withdrawn
   entry is the main case, so `include_inactive` is not needed. Visibility
   (ADR 0011) still applies: the caller only sees what it may read, so a
   fleet change that hides an entry hides it here too.

4. **The caller is keyed as feedback rows are keyed**: the credential's
   `user_id` plus the agent a rating is filed under (the key's agent, or
   `admin` for the admin key; ADR 0066). The read service resolves this
   and hands the store a `(user, agent)` pair on the filter. A credential
   with no agent identity, such as the org key, gets
   `agent_identity_required` (REST 422) or `agent_unresolved` (MCP), as
   `hive_feedback` does. An empty list would read as "nothing changed",
   which is wrong when the call could not know who is asking.

5. **Each item shows why it is listed.** A list item already carries
   `state` and `superseded_by`. On MCP, when this filter is on, items also
   carry their `feedback` counts, so a `wrong` report on a still-active
   entry is visible without opening it. REST returns full entries, which
   have `state`; the counts are on `GET /v1/entries/{id}`.

6. **The order is the list's usual order** (newest entry first). No order
   by change date is added, for the reason ADR 0054 gives.

7. **Display and filter only.** No score, rank, quality multiplier or
   guardrail reads this filter, so the invariants of ADR 0070 are
   unaffected.

8. **Each store applies it beside `matches`**, as with `flagged`: a
   correlated `EXISTS` from the entry to the caller's `helpful` row in
   Postgres, a lookup in the live feedback rows in `MemoryStore`. A shared
   contract test runs the cases against both.

9. **Withdrawals get a time.** Migration 0016 adds a nullable
   `entries.withdrawn_at`, set from the database clock when an entry is
   withdrawn (`MemoryStore` stamps its own clock). Existing withdrawn rows
   are backfilled with the migration's `now()`, meaning "withdrawn by the
   time this shipped, at the latest": a `helpful` rating older than that
   lists them once, and a later one clears them. The migration is
   expand-only: a nullable column with no default, so an older pod ignores
   it. A withdrawal an older pod writes during a rolling deploy leaves it
   NULL, which the filter reads as "withdrawn after any rating". The
   column is not on the wire.

10. **No new index.** The planner starts from the caller's `helpful` rows
    (the primary key is `(entry_id, "user", agent)`, so the lookup by
    caller is a scan of `feedbacks`). Measured on 50,000 entries and
    600,000 feedback rows (about ten times the expected scale of 300
    agents and tens of thousands of entries), the query took 47 ms with a
    parallel sequential scan of `feedbacks`, and 36 ms with a partial index
    on `("user", agent) WHERE verdict = 'helpful'`. The work is dominated
    by the per-row checks on the caller's roughly 1,500 helpful entries,
    which an index does not remove. An index would cost every feedback
    write for about 10 ms on a catch-up call. If the feedback table grows
    far past that, add the partial index then, as an expand-only
    migration.

11. **The skill's catch-up step** lists `hive_list` with
    `relied_on_changed: true` for any agent that can read, not only
    writers, and says how an item clears: re-check, then rate it again. The text shares the always-on byte budget, which is not
    raised: the two catch-up list lines were merged and wording said twice
    was dropped.

## Consequences

- An agent that built on an entry learns, at its next catch-up, that the
  entry changed, and can re-check what it built.
- An item clears when the agent re-checks and rates it again after the
  change: `helpful` if it still holds (its `updated_at` moves past the
  change), `stale` or `wrong` if not (the `helpful` row is replaced).
  Every change kind clears this way, withdrawals included.
- Ratings are the only dependency record. An agent that used an entry and
  rated nothing is not told; the skill already asks agents to rate what
  their work tested.
- A `wrong` report that is itself mistaken still lists the entry. The
  report's note says whether it holds.
- This is a filter, not a notification channel: nothing is pushed.
