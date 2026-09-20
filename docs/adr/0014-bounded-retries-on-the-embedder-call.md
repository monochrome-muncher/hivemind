# Bounded retries on the embedder call

The write path is **inline**: `WriteService.write` embeds the entry and
then inserts it (SPEC §7 — embeddings are generated at write time). The
embedder call is an external HTTP dependency (OpenAI-compatible
endpoint, ADR 0005) with a 10-second timeout, and it had **zero
retries** — a single transient blip (a local vLLM hiccup, a provider
5xx, a momentary connection reset) failed the whole write, and the
memory the agent wanted to store was lost.

At Hivemind's scale (a few writes/second at peak, ~300 agents across
multiple fleets), a small bounded retry budget is the cheapest
reliability win available — it needs no new service, no queue, and no
change to write semantics.

## Decision

The OpenAI-compatible embedder retries **transient** failures with a
bounded, exponential backoff:

* **Retried:** request timeouts, connection/network errors, `429`, and
  `5xx` responses.
* **Fail-fast (never retried):** other `4xx` (400/401/403/422 — a bad
  request or broken credential will not fix itself) and client-side
  validation failures (dimension mismatch — the endpoint is healthy,
  the response is simply wrong for this deployment).
* **Budget:** `HIVEMIND_EMBEDDING_RETRIES` (default **2** retries, i.e.
  up to 3 attempts; `0` disables retrying). Backoff base is 0.5s,
  doubling per retry (0.5s, 1s, ...). The budget is a config value,
  not a code constant (the same "config, not code" rule as the
  retrieval knobs, ADR 0006).

## Consequences

* A transient embedder blip no longer drops a write. A *dead* embedder
  still fails the write — after the full budget (at most ~1.5s of
  backoff plus up to 3 × 10s timeouts) — so the failure semantics are
  unchanged: a write either lands fully (vector + entry) or fails.
* This is a **reliability** fix, not an async pipeline: embedding stays
  inline on the write path (SPEC §7). The "persist without a vector +
  catch-up" fallback (a Postgres outbox with `FOR UPDATE SKIP LOCKED`)
  remains a trigger-gated extension (ROADMAP Tier 5, "Async embedding
  pipeline"): it changes write semantics (an entry is
  vector-searchable only after its vector lands) and is only justified
  if embedder outages become common enough that losing a write is
  unacceptable.