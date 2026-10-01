# Search falls back to keywords when the embedder is down

Amends ADR 0041, decision 4 (typed embedder failure on reads).

## Context

ADR 0041 made a failed query embedding a typed error on search:
`502 embedding_unavailable` on REST and the `embedding_unavailable` code
from MCP `hive_search`. It left a keyword-only fallback for a separate
decision, because a fallback changes result quality without the caller
asking for it (ROADMAP §3.13).

Search is the read most agents make before they start work. When the
embedding service is down, every search fails, although the keyword
stream (SPEC §6.2) needs no embedding and the store is healthy. Agents
then work without any shared memory until the service returns.

## Decision

1. **When the query embedding fails with an `EmbeddingError`, search
   continues with the keyword stream alone.** The vector stream is
   treated as empty, and RRF fusion, rescoring, the supersession invariant
   and pagination run as usual. The embedder's own retries and deadline
   (ADRs 0014 and 0041) run first, so this happens only after they are
   spent.

2. **The degraded answer says so**, so the caller never mistakes it for a
   full search:
   - REST `POST /v1/search` answers 200 with the usual body and the header
     `X-Hivemind-Degraded: keyword-only`. The body stays a list of hits,
     so existing clients keep working.
   - MCP `hive_search` adds `"degraded": "keyword_only"` and a short
     `note` to its result. The tool description and the plugin skill tell
     agents that a thin or empty degraded result does not show that
     nothing was recorded.
   - The service logs a warning with the (already sanitized) cause.

3. **Only the embedding failure is absorbed.** A store failure in either
   stream still propagates (`503 store_unavailable` on a timeout), and the
   other stream is cancelled as before.

4. **Writes are unchanged**: a write still needs its embedding and still
   answers `502 embedding_unavailable` (ADR 0041), so an entry either lands
   fully or not at all.

## Consequences

- Search keeps working through an embedding outage, without semantic
  matches. A query that shares no word with the right entry misses it.
- Fused scores of a degraded page are about half those of a healthy page,
  because only one stream contributes. Scores are only compared within a
  page, so the order is unaffected.
- A misconfigured embedder (a wrong dimension, for example) now degrades
  search quietly instead of failing it. Writes still fail loudly, and the
  warning is logged on every degraded search.
- There is no setting to turn the fallback off. One can be added if an
  operator needs searches to fail instead.
