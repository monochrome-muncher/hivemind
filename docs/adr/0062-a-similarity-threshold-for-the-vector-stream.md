# A similarity threshold for the vector stream

## Context

The vector stream returns the entries nearest to the query, however far
away they are. [ADR 0061](0061-vector-search-needs-positive-similarity.md) left out entries with a similarity of zero or
below, but with a real embedding model almost every entry has a positive
similarity to almost every query, so nearly every search returns hits. An
agent cannot tell "the hive has an answer" from "the hive has nothing on
this", and the empty-search counter (ADR 0056), meant to show what agents
look for and fail to find, rarely fires.

A similarity cut-off fixes both, but its value depends on the embedding
model (and on its dimension, for models that truncate): the similarities
one model gives related text are not another's. The repository has no
real queries to measure against, only the synthetic golden and
age-varied sets (`tests/eval/`).

## Decision

1. **A setting, off by default.** `SearchConfig.vector_min_similarity`
   (`HIVEMIND_VECTOR_MIN_SIMILARITY`), in `[0, 1)`. Unset, empty or `none`
   means off: the vector stream keeps every entry with a positive
   similarity, exactly as before. Out-of-range values fail at startup
   (ADR 0024).

2. **Vector stream only.** When set, `Store.search_vector` leaves out
   entries whose cosine similarity to the query is at or below it, on both
   adapters (`min_similarity`, covered by the Store contract suite). The
   keyword stream is untouched, so an entry sharing a word with the query
   still comes back, and a search is empty only when neither stream finds
   anything. `PgStore` drops the rows from the top-k the index returns,
   as for ADR 0061, so the index scan does not change. `similar_entries`
   (related entries on write, ADR 0052) is not affected.

3. **Measured per deployment.** `tests/eval/threshold.py`
   (`make measure-threshold`) embeds the golden and age-varied corpora with
   the configured embedder, adds off-topic queries no entry answers, and
   reports for a sweep of thresholds how many answers stay in the vector
   stream, hit@5, and how many off-topic searches come back empty. It
   suggests the highest 0.05 step at least 0.05 below the weakest answer,
   since a missed answer costs an agent more than an unrelated hit.
   `docs/retrieval-experiments.md` describes how to read it.

## Consequences

- Nothing changes until an operator sets the value.
- With a value set, a search with nothing relevant can come back empty
  and is counted by ADR 0056, unless the keyword stream matches a word
  (Postgres FTS drops stop words, so filler words do not count).
- A value set too high drops real answers from the vector stream; the
  keyword stream then carries them only if they share a word with the
  query. Changing the embedding model or its dimension means measuring
  again.
- The synthetic sets are small (24 entries, 18 answerable and 12
  off-topic queries); a value measured on them is a starting point, to be
  checked against the empty-search counter and agents' feedback.
