# The keyword stream matches any query term

Settles the keyword-stream semantics left open in ROADMAP §3.13.

## Context

The two stores disagreed about which entries the keyword stream returns
(SPEC §6.2). `PgStore.search_keyword` used `plainto_tsquery`, which joins
the query's words with `&`, so an entry matched only if it contained every
word. `MemoryStore.search_keyword` matches an entry that shares any word
with the query. The unit suite and the golden eval run on `MemoryStore`,
so they measured a recall that production never had.

Agents search in natural language ("which embedding dimension is best for
semantic search"). Such a query almost always carries a word the right
entry lacks, and the every-word rule then drops it from the keyword stream
entirely. Measured on the golden set (`tests/eval/golden.py`), with the
production `search_tsv` and `ts_rank`, keyword stream only:

| Rule       | hit@5 | MRR   |
|------------|-------|-------|
| every term | 1 / 8 | 0.125 |
| any term   | 8 / 8 | 1.000 |

## Decision

1. **An entry matches the keyword stream when it contains any query
   term**, on both stores. `ts_rank` still orders the matches, and an
   entry matching more terms ranks higher.

2. **Postgres builds the query as
   `replace(plainto_tsquery('english', q)::text, ' & ', ' | ')::tsquery`**
   (`pgstore.ANY_TERM_TSQUERY`). `plainto_tsquery` still parses the
   caller's text, so no operator syntax gets through. The rewrite only
   swaps the separators between quoted lexemes, and the `english` parser
   never produces a lexeme containing a space. `websearch_to_tsquery` was
   not chosen: it reads `or`, `-` and quotes as operators, which turns
   agents' natural-language text into syntax.

3. **`MemoryStore` is unchanged**: it already matches any term.

## Consequences

- Keyword recall on Postgres rises sharply for natural-language queries.
  RRF fusion (SPEC §6.2) sees a keyword rank for far more relevant entries.
- More rows match a multi-word query, so `ts_rank` sorts more of them.
  On a synthetic 100k-row table, a query matching 25% of rows took 12 ms,
  and one matching every row took 24 ms. The old rule took 26 ms on a
  query that matched every row, so the worst case is unchanged. ROADMAP
  §4.1 (BM25 / FTS) still owns that worst case.
- The stores still tokenize differently (Postgres stems with the `english`
  configuration and indexes summary and body; `MemoryStore` splits words
  and also matches tags). They now agree on any versus every term, not on
  every token.
- `tests/integration/test_pgstore.py` keeps the golden-set measurement as
  a gate: the Postgres keyword stream must put the relevant entry in its
  top 5 for every golden query.
