# Provider calls: an overall deadline, jittered backoff, and typed embedder errors

ADR 0014 bounded the embedder's retries (and ADR 0016 copied the pattern
for the extractor), but left four gaps that the provider review found
(PC-1, PC-2/SP-7/MCP-2, PC-5, PC-12):

- httpx's `timeout` is **per I/O phase**, so one slow-drip response could
  outlive it, and retries multiplied the overshoot; a write could sit
  inline for minutes, long enough that a client gave up and retried a
  non-idempotent `POST /v1/entries`.
- Backoff was fixed, so replicas that failed together retried together,
  and a provider's `Retry-After` was ignored.
- A dropped keep-alive (`RemoteProtocolError`, the first call after an
  idle period) was treated as deterministic and failed the write.
- Search did not handle `EmbeddingError` at all (REST 500, MCP opaque
  tool error), although writes answered 502; and error text built from
  httpx exceptions could carry the `Authorization` header (an API key
  with a trailing newline produced `Illegal header value b'Bearer <key>'`).

## Decision

Amending ADR 0014 (and the ADR 0016 extractor pattern):

1. **Overall deadline per provider call**, retries and sleeps included
   (`asyncio.timeout`). `HIVEMIND_EMBEDDING_DEADLINE` /
   `HIVEMIND_EXTRACTOR_DEADLINE`; **unset means derived**:
   `(retries + 1) x timeout + 0.5 x (2^retries - 1)`, the documented
   DEPLOY.md §5 worst case, so existing `*_TIMEOUT` / `*_RETRIES` recipes
   and the pod grace-period arithmetic keep holding. An explicit value
   below the per-attempt timeout is a startup error.
2. **Jittered backoff** (each sleep scaled to 0.5x-1x) and a **`Retry-After`
   honoured up to a 10 s cap** (the deadline still bounds the total).
3. `RemoteProtocolError` is **transient**. Other 4xx (except 429), redirects,
   non-JSON or malformed bodies, non-finite / non-numeric vectors, a wrong
   count or dimension, and client-side encode/URL errors fail fast.
4. **Typed embedder failure on reads too**: `POST /v1/search` answers
   `502 embedding_unavailable` (as the write does) and MCP `hive_search` /
   `hive_write` return the `embedding_unavailable` error code. The message
   is fixed; provider detail is logged, never returned. Search does **not**
   degrade to keyword-only: that would silently change result quality and
   needs its own decision.
5. **Errors never carry secrets**: transport failures are reported by
   exception class name only; API keys are whitespace-stripped and must be
   printable ASCII; endpoint URLs are stripped and logged without userinfo.

## Consequences

- A write's provider wait is bounded by the derived (or configured)
  deadlines; operators lowering `*_DEADLINE` trade tolerance for latency.
- The `Retry-After` cap means a provider asking for a longer pause is
  retried sooner than it asked; the deadline keeps that bounded.
- Keyword-only search fallback remains open.
