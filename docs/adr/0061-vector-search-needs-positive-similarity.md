# Vector search needs a positive similarity

## Context

The two `Store` adapters disagreed on the vector stream. `MemoryStore`, which
the unit tests and the retrieval eval run on, left out entries whose cosine
similarity to the query was zero or below. `PgStore` ranked every embedded
entry, however unrelated, so on Postgres the vector list was always filled
from the visible pool up to the candidate top-k. The Store contract suite
carried this as a known divergence.

The retrieval experiments (the recency-floor and `rrf_k` sweeps) and the
empty-search counter tests (ADR 0056) were all measured on `MemoryStore`, with
the cut-off. Taking the cut-off out of `MemoryStore` changes their results;
putting it into `PgStore` keeps them true of production.

## Decision

`search_vector` leaves out entries with cosine similarity `<= 0` (cosine
distance `>= 1`) on both adapters. `PgStore` drops them from the top-k rows the
HNSW index returns: they sort last, so this equals filtering first, and the
index scan is unchanged (no distance predicate in the `WHERE`).
`similar_entries` (ADR 0052) is not affected.

## Consequences

- An entry pointing away from the query no longer reaches the fused list
  through the vector stream; it can still arrive through the keyword stream.
- With a real embedding model, similarities at or below zero are rare, so
  production result lists barely change. This does not make the empty-search
  counter (ADR 0056) a "nothing relevant" signal: a search still finds
  something whenever any visible entry has a positive similarity. A tuned
  relevance threshold would be a separate decision.
- The retrieval experiments keep measuring what production does.
