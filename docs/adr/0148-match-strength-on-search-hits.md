# A search hit says how close a match it is

Builds on [ADR 0062](0062-a-similarity-threshold-for-the-vector-stream.md) and [ADR 0100](0100-vector-similarity-cut-off-on-by-default.md) (the vector stream's similarity), and [ADR 0102](0102-trim-search-hit-score-and-empty-feedback.md) (compact hits). Changes the `Store.search_vector` port, the search hit (SPEC §6.1) and the search reply. Ranking is unchanged.

Status: Accepted.

## Context

`score` is the reciprocal-rank fusion of the two candidate streams, so it measures rank, not relevance: the top hit scores about 0.0164 for every query, a perfect match and an unrelated one alike. An agent has no way to tell a close match from the least bad of a weak result list.

A live check on 2026-11-12 showed the cost. Qwen3-14B in OpenCode, asked to brainstorm, searched, got one loosely related hit and answered from it as if it were evidence. The cosine similarity that would have told the two apart was computed in `PgStore.search_vector` (`1 - distance`) and thrown away.

## Decision

1. **Every hit carries `match`**: `both`, `keyword` or `vector`, by which candidate streams found it. It is always present.
2. **A hit carries `similarity`** when the vector stream found it: the cosine similarity of the query to the entry, rounded to 2 decimals. It is omitted over MCP and `null` over REST for a keyword-only hit and for every hit of a degraded search (ADR 0048). A keyword-only hit has no similarity to show; it is not given a made-up one.
3. **The reply says when nothing is close.** When the search is not degraded, has hits and no hit on the page has `similarity >= close_match_similarity`, both as shown (rounded to 2 decimals, so a hit at 0.646 shows 0.65 and counts as close with the default bar) and judged per page, `hive_search` adds `"closest": "loose"` and a `note` ("No hit is a close match; treat these as loosely related and check before relying on them."), and REST sets the header `X-Hivemind-Closest: loose`, the way `degraded` and `X-Hivemind-Degraded` already travel. A degraded search keeps its own note and gets no loose one. `SearchResult.loose` is computed once in the search service, so MCP and REST agree.
4. **`HIVEMIND_CLOSE_MATCH_SIMILARITY`** sets the bar: a float in `[0, 1)`, default 0.65, `0` turns the note off. Like `HIVEMIND_VECTOR_MIN_SIMILARITY` it is model-specific. The default is calibrated for Qwen3-Embedding-8B, the production embedder; another model needs its own value (`make measure-close-match`, see Calibration).
5. **`Store.search_vector` returns `VectorMatch(entry_id, similarity)`** instead of bare ids, in the same order and with the same `min_similarity` filter. `MemoryStore` already computed the same cosine similarity; both adapters now return it.
6. **The agent text gains one clause** in `hive_search`'s description, paid for by trimming wording elsewhere in the always-on text (ADR 0085 budget).

## Consequences

- An agent, and the user reading its transcript, can see how strong each hit is. A model that ignores the note still sees `match` and `similarity` on each hit.
- `match: "keyword"` does not mean the entry is dissimilar to the query: it may sit outside the vector candidates (top-k, the approximate index), so a keyword-only hit can make a page loose.
- The flag is judged per page: a later page, with other hits, can differ from the first.
- Ranking, scores and the vector cut-off do not change; the `score` field keeps its meaning and its rounding.
- A contract change on the MCP and REST hit: two new fields and one new reply key or header. Clients that ignore unknown fields are unaffected; the REST hit's `match` is required in the response model.
- The similarity is the stored vector's cosine against the (instructed) query vector, so it moves with the embedding model and with `HIVEMIND_QUERY_INSTRUCTION` (ADR 0097). Changing either means re-checking the bar.
- HNSW is approximate (ADR 0025): the similarity of a hit is exact for that hit, but a close neighbour can be missing from the candidates, so "no close match" is evidence, not proof.
- The admin and portal search views do not show `similarity` yet.

## Calibration

Measured on 2026-11-12 with Qwen3-Embedding-8B (Q8_0 GGUF on CPU, last-token pooling, truncated to 1024 dimensions and renormalised, as vLLM serves it), the query instruction on and the default body prefix, over the sixteen-domain eval set: `make measure-close-match` (`tests/eval/close_match.py`). "Top" is the similarity of the nearest entry, the best `similarity` a page could show. A one-domain pool stands for one fleet: 128 entries, its 40 to 42 answerable queries, its own unanswered queries (30 over the sixteen pools, the hard case: same subject, no answer) and every other domain's queries.

| One-domain pools, top similarity | median | 10th pct | 90th pct |
|---|---|---|---|
| Answerable, vague queries | 0.79 | 0.66 | 0.87 |
| Answerable, specific queries | 0.87 | 0.80 | 0.92 |
| Unanswered, same domain | 0.58 | 0.44 | 0.69 |
| Unanswered, other domains | 0.47 | 0.37 | 0.58 |

| Bar | Answerable marked loose (vague / specific) | Unanswered marked loose (same domain / other) |
|---|---|---|
| 0.55 | 1.4% / 0.2% | 40% / 83% |
| 0.60 | 3.9% / 0.3% | 53% / 92% |
| **0.65** | **8.7% / 0.6%** | **73% / 97%** |
| 0.70 | 17.8% / 1.5% | 93% / 99% |

0.65 is the default. The case this ADR is for is the same-domain miss, a hit on the right subject that does not answer: 0.65 flags three in four of those and nearly every off-subject one, while marking about one vague answerable query in eleven (and almost no specific one) loose. A wrong flag costs a check, since the note asks the agent to verify, not to discard; a missed one lets a loose hit pass as evidence. 0.70 catches more misses but marks one vague answerable query in six. The same-domain sample is small (30 queries), so production data may move the bar.

With all 2,048 entries in one pool, some entry usually sits near any query (unanswered median top 0.66), so 0.65 flags 43% of unanswered queries there, with the same false-loose rate (7.6% vague, 0.3% specific). A bigger pool makes the note rarer, not wrong.
