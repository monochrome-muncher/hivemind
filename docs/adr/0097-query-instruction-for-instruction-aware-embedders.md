# A query instruction for instruction-aware embedders, on the query side only

Amends [ADR 0005](0005-fixed-dimension-pgvector-with-operator-configured-embeddings.md) and SPEC §7 ("what is embedded"). Adds one setting, `HIVEMIND_QUERY_INSTRUCTION`. Does not change how any entry is embedded.

Status: Accepted. Off by default until a deployment measures it with its own embedder.

## Context

Qwen3-Embedding, the embedder this project is measured on, is instruction-aware. Its model cards say a query should be embedded as `Instruct: {task}\nQuery:{query}` (Qwen's own example code writes exactly that, with no space after `Query:`) and a document with no instruction. Leaving the query instruction out costs about 1 to 5% of retrieval quality, by the model cards' account.

Today `SearchService` embeds the raw query with `embed_text`, the same call that embeds entry text. A search is therefore always the uninstructed case.

## Decision

1. **A setting.** `SearchConfig.query_instruction`, from `HIVEMIND_QUERY_INSTRUCTION` (a `Settings` field, so the strict-env rule of ADRs 0024 and 0032 covers it). Empty or unset is off, and off is today's behaviour exactly.
2. **Query side only.** When set, the text the vector stream embeds is `Instruct: {instruction}\nQuery:{query}`. Documents never carry an instruction. Entry embeddings, the `related` entries and `near_duplicate` warning on a write, version grouping and explore similarities are entry against entry and are unchanged. The keyword stream keeps the raw query. The seam is `SearchConfig.embedded_query`, called by the vector stream; the `Embedder` port is untouched, so no adapter needs to know about it.
3. **Validation.** The value is stripped, must be a single line (no newline or carriage return) and at most 500 characters, and has no NUL. A bad value fails at startup, like the other search settings. It is checked with the shared input bounds of ADR 0040.
4. **One composition root.** The setting rides on `SearchConfig`, which `build_services` already hands to the REST app, both stdio MCP runners and the HTTP MCP runner. The admin search and the portal search go through the REST search, so they get it too.
5. **Embedder-specific, and off by default.** The format is Qwen3's. Another embedder may not expect it and can be hurt by it. So a deployment turns it on only after `make measure-query-instruction` (`tests/eval/query_instruction.py`) shows a gain with its embedder. The harness runs every answerable query of the sixteen domain fixtures, alone and pooled, with no instruction, with a generic one (`Given an agent's task or question, retrieve shared memory entries that answer it`) and with any variants passed in `EVAL_QUERY_INSTRUCTIONS`, and reports hit@1, hit@5 and MRR with the change against no instruction.
6. **Re-check the similarity threshold.** An instruction shifts query vectors, so the cosine similarity of the right answer moves. The harness reports the mean and 10th percentile of that similarity for the pooled fixture, per instruction. `HIVEMIND_VECTOR_MIN_SIMILARITY` (ADR 0062; 0.45 recommended for Qwen3) may need to be re-measured with `make measure-threshold` after turning the instruction on.

The agent-facing text (skill, tool descriptions, server instructions) does not change.

## Consequences

- Nothing changes for a deployment that does not set it.
- Turning it on changes search ranking, not stored data: no re-embedding, and it can be turned off again at once.
- The instruction is part of the query text the embedder sees, so it counts against the embedder's input limit (at most 500 characters more than the 2,000-character query bound).
- The default stays off in this ADR. Making it the default for Qwen3 deployments would be a later decision, after a measurement recorded in `docs/retrieval-experiments.md`.
