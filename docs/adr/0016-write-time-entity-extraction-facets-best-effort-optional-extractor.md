# Write-time entity extraction: best-effort, optional, schema-validated

Hivemind's knowledge-graph row (SPEC §10) has been trigger-held since
ADR 0006 — "no knowledge graph, no entity extraction, no
auto-contradiction detection" is an explicit v1 non-goal (SPEC §9).
The org's scale ambition (300+ agents across multiple fleets — the
updated note in the ROADMAP Tier 5 section) makes the *facet-level*
slice of that row worth shipping **before** the trigger fires: an agent
asking "everything about the auth service" should be able to filter by
a name facet, not only by keyword/vector similarity. The facet slice is
the one part whose payoff is measurable with the §1.1 eval harness —
graph expansion (multi-hop) stays trigger-held. This ADR is therefore a
**pre-staged** §10 extension, recorded honestly as ambition-driven,
not as a trigger that fired.

## Decision

* **Facets, not a graph.** A fixed-prompt LLM extractor runs **at
  write time** over the same text the embedder sees (`summary` + the
  bounded body prefix, `HIVEMIND_EMBEDDING_PREFIX_CHARS`). Its output
  is validated **all-or-nothing** against a fixed schema:
  `entities: [{name, kind}]` where `name` is open vocabulary
  (trimmed, non-empty, ≤ 128 chars), `kind` is a **closed** vocabulary
  (`person | organization | system | service | artifact | concept`),
  ≤ 10 entities per entry, deduped. Malformed output ⇒ the call
  fails (no partial salvage).
* **Storage symmetric with the embedding pair (ADR 0005):**
  `entries.entities jsonb` + `entries.entities_model text` — one
  machine writer, immutable after write (ADR 0001), provenance via
  `entities_model`. No separate table, no registry, no cross-entry
  linking (that is the next step — canonicalization + graph — and it is
  *not* in this ADR).
* **A new `Extractor` port, sibling to `Embedder`:** `Extractor`
  protocol + `OpenAICompatExtractor` (httpx POST to `/chat/completions`,
  fixed prompt, structured JSON response) + `build_extractor(settings)`
  factory. Response validation uses the repo's existing Pydantic v2
  dependency — **no Pydantic AI** (an agent framework is the wrong
  shape for "one fixed prompt, one structured response"). Retries
  follow ADR 0014 (transient-only, bounded backoff;
  `HIVEMIND_EXTRACTOR_RETRIES`, default 2; `0` disables). The
  extractor's model is a **deploy-time decision** (the ADR 0005
  stance): e.g. a Qwen3-27B-class chat model on a *separate service*
  from the embeddings server (dev/test: `http://localhost:8080/v1`,
  model `qwen3.8-27b`).
* **Optional, then best-effort.** `HIVEMIND_EXTRACTOR_ENDPOINT`
  unset ⇒ `build_extractor` returns `None` ⇒ `WriteService` skips
  extraction (entries land with `entities: []`): a deployment with no
  chat model keeps the full system at zero LLM-extraction cost — the
  same dev-mode stance as "no authenticator = dev mode". An extraction
  **failure** (timeout, retry exhaustion, schema mismatch) never
  blocks the write: the entry lands with `entities: []` and no
  `entities_model`. Entry-level write semantics are unchanged (ADR
  0014); only the enrichment is lost. An async outbox fallback stays
  trigger-held (the Tier 5 "async embedding pipeline" row is the
  template).
* **Query surface: a name facet.** `EntryFilters.entities` —
  AND-semantics over names, case-insensitive (mirrors `tags`);
  exposed on REST `GET /v1/search` + `GET /v1/entries` and
  `hive_search` / `hive_list`. `kind` is stored + displayed, not
  filterable in v1.
* **Machine-only.** Agents declare facets via `tags` (the existing
  channel); `entities` is a pure machine signal with a single writer
  (`entities_model` provenance). An agent-supplied `entities`
  parameter is a later, explicitly-specified extension (it would need
  a source flag).
* **No backfill.** Entries written before this ships keep
  `entities: []`; an operator-run backfill is a named maintenance
  procedure (the ADR 0005 re-embedding-migration pattern — the one
  sanctioned in-place exception to ADR 0001, for machine metadata
  only), documented in the runbook. No CLI ships with this ADR; it is
  built when the pool size justifies it.

## Consequences

* The SPEC §9 "no entity extraction" non-goal becomes a commitment for
  the **facet slice** (SPEC §13); ADR 0006's rejection of
  *graph-expanded* retrieval stands unchanged — facets are not a
  graph, and multi-hop expansion + the canonical entity registry
  remain trigger-held Tier 5 extensions.
* The write path gains a second external LLM dependency, bounded by
  the optional + best-effort stance: deployments without a chat model
  are fully supported, and extraction adds at most one bounded chat
  call (`HIVEMIND_EXTRACTOR_TIMEOUT`, default 30 s) to the write path.
* The schema gains two columns (`entities jsonb NOT NULL DEFAULT
  '[]'`, `entities_model text`) — an idempotent migration + a new
  `SCHEMA_VERSION` generation (ADR 0013).
* Dev/test endpoints are available (`http://localhost:8080/v1`,
  `qwen3.8-27b`, API key `dummy`), so the extractor path is
  end-to-end testable without a production model.
* The ROADMAP records this as a **pre-staged §10 extension** (Tier
  4.3): the §10 knowledge-graph trigger has *not* fired — it is built
  on scale ambition, and its facet payoff is measured with the §1.1
  harness.