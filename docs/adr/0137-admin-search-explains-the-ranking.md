# The admin search can explain the ranking

Extends [ADR 0080](0080-admin-memory-browser-and-activity.md) (the admin search) and builds on
[ADR 0136](0136-admin-search-as-an-agent.md) (search as an agent), [ADR 0022](0022-recency-floor-so-match-quality-is-the-sort-key.md)
(the recency floor), [ADR 0063](0063-feedback-quality-by-consensus-share.md) (feedback quality),
[ADR 0070](0070-one-capped-budget-for-social-signals.md) (the social cap),
[ADR 0073](0073-a-recency-hint-on-search.md) (the recency hint) and [ADR 0048](0048-search-falls-back-to-keywords-when-the-embedder-is-down.md)
(keyword-only search).

Status: Accepted.

## Context

A hit carries one `score`. Whether the ranking needs another change is decided from what happens in
production, and nobody can see why a hit landed where it did: was it the keyword match, the vector
match, the age of the memory, the feedback, the importance? The numbers are in the service for a moment
and then gone. Reading them back by re-running the formula by hand is slow, and a second copy of the
formula in a tool would drift from the real one.

## Decision

1. **`POST /v1/admin/search` takes an optional `explain` (default false).** When true, each hit carries
   an `explain` object with the parts of its score as the ranking computed them. It also works with
   `as_agent`, so the agent's own search is the one explained. The data-plane `POST /v1/search`, the
   portal search and the MCP tools do not have it: `HitOut`, the MCP hit and the REST request model
   are unchanged, and an agent that sends `explain` gets the ordinary answer. With `explain` off the
   admin response is byte for byte what it was.

2. **The parts come from the code that ranks.** `retrieval.scoring.score_parts` computes the factors
   and their product, and `entry_score` is now `score_parts(...).score`, so there is one formula. The
   parts are: the fused RRF value, the importance factor, the recency factor (with the memory's age,
   the half-life, the decay before the floor, the floor and whether it bound, and which recency hint was
   applied), the feedback quality (with the counts behind it and whether a confirmation halved the
   flags), the social factor `min(social_max, quality)` (with whether the cap bound), and the score.
   Beside them: the entry's 1-based rank in the keyword stream and in the vector stream (null when
   absent), and the mode, `keyword_only` when the embedder failed (ADR 0048). A unit test holds that the
   parts multiply to the score the service returns, for every hit.

3. **Only when asked.** The stream ranks are built and the explanation objects are kept only when
   `explain` is true. No query is added. The scoring itself is the same few multiplications as before.

4. **Display only.** The explanation is read back from the ranking and never feeds it. Supersession
   order (SPEC 6.3), version grouping and `explore` change the order, not a score, so
   they are not parts. The vector similarity is not shown: the store returns ranked ids, and exposing
   the distance would change the `Store` port for a number the fused value already stands for.

5. **The Memories view has an "Explain ranking" checkbox**, kept in the hash as `explain=1` and used
   in search mode. When on, each hit has a line list under its row in plain words ("Vector: rank 3",
   "Recency x0.93 (memory date 40 days ago)", "Feedback x1.05 (2 helpful)", "Score 0.0312"), drawn
   with `textContent` only. With an agent named, the agent's search is explained. The CSV export and
   the "Hidden from" card do not ask for it.

## Consequences

- An operator can read the ranking of a production query: why one memory beat another, whether the
  recency floor or the social cap bound, whether the answer was keyword-only.
- Parts are shown to six significant figures and the score to four, so a reader multiplying them gets
  the score within rounding.
- The explanation exposes how an agent's search is weighed, so it is admin only and an agent key gets
  403 on the admin route.
- A change to the formula changes the parts with it; the wording in `explain.js` follows the response
  and has no formula of its own.
- Nothing is stored and no migration is needed.
