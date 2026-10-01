# The keyword stream matches at most 16 distinct query terms

Amends ADR 0047 (the keyword stream matches any query term): bounds it,
and corrects its cost measurement.

## Context

ADR 0047 made the Postgres keyword stream match entries containing any
query term. `ts_rank` scores every matching row once per query term, so
the cost of one search is roughly (rows matched) × (query terms). Under
the every-term rule a long query matched almost nothing; under the
any-term rule one common word is enough to match most of the pool, and
`check_query` (ADR 0040) allows 2 000 characters, which is a few hundred
words.

ADR 0047 measured 24 ms for a query matching every row on a synthetic
100k-row table. That table's entries were short. Measured again on 100k
entries with 120-word bodies and a Zipf-distributed vocabulary (keyword
stream only, `LIMIT 20`, Postgres 16, median of 3):

| Query                                   | every term (2.0.x) | any term (2.1.0) |
|-----------------------------------------|--------------------|------------------|
| 2 rare words                            | 0.7 ms             | 1.8 ms           |
| 5 words, one of them common             | 0.9 ms             | 225 ms           |
| 2 000 characters of natural text        | 6.7 ms             | 2 136 ms         |
| 300 common words (crafted, < 2 000 chars) | —                | 7 125 ms         |

Cost grows linearly with the number of terms (1 term 85 ms, 16 terms
552 ms, 64 terms 1 603 ms, 128 terms 3 138 ms on the same table). Any
agent that can read can send the crafted query, and a handful of them at
once holds the database's CPU.

## Decision

1. **The keyword stream matches on the first 16 distinct terms of the
   query** (`pgstore.MAX_KEYWORD_TERMS`), in query order, after
   `plainto_tsquery` has parsed, stemmed and removed stop words. A
   repeated term takes one slot. Later terms are ignored by the keyword
   stream only; the vector stream still embeds the whole query.

2. The tsquery is built in one scalar subquery, so Postgres evaluates it
   once per statement. The injection argument of ADR 0047 is unchanged:
   `plainto_tsquery` does the parsing, and the rewrite only splits on its
   `' & '` separator and re-joins with `' | '`.

3. `MemoryStore` is unchanged. It is the dev and unit-test store, where
   cost does not matter; the two stores already tokenize differently
   (ADR 0047).

## Consequences

- The worst case falls from seconds to about six times the cost of a
  one-term query (2 136 ms to 542 ms for the 2 000-character query
  above). A natural-language query rarely has more than 16 terms after
  stop-word removal, so typical results do not change; the golden-set
  gate (`test_keyword_stream_finds_every_golden_entry`) still passes.
- The remaining cost is linear in the rows a common term matches, as it
  was for a one-word query before ADR 0047. Bounding that needs a ranker
  with document frequency (IDF) or a cap on ranked rows, which is ROADMAP
  §4.1 (BM25 / FTS).
