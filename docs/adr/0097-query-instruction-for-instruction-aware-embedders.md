# A query instruction for instruction-aware embedders, on the query side only

Amends [ADR 0005](0005-fixed-dimension-pgvector-with-operator-configured-embeddings.md) and SPEC §7 ("what is embedded"). Adds one setting, `HIVEMIND_QUERY_INSTRUCTION`. Does not change how any entry is embedded.

Status: Accepted. On by default with the generic instruction; set `HIVEMIND_QUERY_INSTRUCTION` to an empty value to turn it off (for example with a non-Qwen embedder).

## Context

Qwen3-Embedding, the embedder this project is measured on, is instruction-aware. Its model cards say a query should be embedded as `Instruct: {task}\nQuery:{query}` (Qwen's own example code writes exactly that, with no space after `Query:`) and a document with no instruction. Leaving the query instruction out costs about 1 to 5% of retrieval quality, by the model cards' account.

Today `SearchService` embeds the raw query with `embed_text`, the same call that embeds entry text. A search is therefore always the uninstructed case.

## Decision

1. **A setting.** `SearchConfig.query_instruction`, from `HIVEMIND_QUERY_INSTRUCTION` (a `Settings` field, so the strict-env rule of ADRs 0024 and 0032 covers it). Unset means the default, `DEFAULT_QUERY_INSTRUCTION`: `Given an agent's task or question, retrieve shared memory entries that answer it`. An explicitly empty value turns it off, and off is the old behaviour exactly.
2. **Query side only.** When set, the text the vector stream embeds is `Instruct: {instruction}\nQuery:{query}`. Documents never carry an instruction. Entry embeddings, the `related` entries and `near_duplicate` warning on a write, version grouping and explore similarities are entry against entry and are unchanged. The keyword stream keeps the raw query. The seam is `SearchConfig.embedded_query`, called by the vector stream; the `Embedder` port is untouched, so no adapter needs to know about it.
3. **Validation.** The value is stripped, must be a single line (no newline or carriage return) and at most 500 characters, and has no NUL. A bad value fails at startup, like the other search settings. It is checked with the shared input bounds of ADR 0040.
4. **One composition root.** The setting rides on `SearchConfig`, which `build_services` already hands to the REST app, both stdio MCP runners and the HTTP MCP runner. The admin search and the portal search go through the REST search, so they get it too.
5. **On by default for Qwen3, the production embedder family.** The format is Qwen3's, and the measurement below shows a gain on all three sizes. Another embedder may not expect it and can be hurt by it, so a deployment with a different embedder should run `make measure-query-instruction` (`tests/eval/query_instruction.py`) and turn the instruction off if it shows no gain. The harness runs every answerable query of the sixteen domain fixtures, alone and pooled, with no instruction, with the generic one and with any variants passed in `EVAL_QUERY_INSTRUCTIONS`, and reports hit@1, hit@5 and MRR with the change against no instruction.
6. **The similarity threshold.** An instruction shifts query vectors, so the cosine similarity of the right answer moves. It lowers answer similarity slightly but off-topic similarity much more, so `HIVEMIND_VECTOR_MIN_SIMILARITY` separates answers from off-topic searches better than before. 0.45 (ADR 0062) stays the recommendation for Qwen3. The threshold itself stays off by default, as ADR 0062 decided. The harness reports the mean and 10th percentile of the answer similarity per instruction, and `make measure-threshold` re-measures the threshold.
7. **One instruction for every route.** Search-aware strategies were measured on the 4B model: different instructions for question-, error- and topic-shaped queries, and kind-specific instructions together with a kind filter. Neither beat the single generic instruction, so every search route uses the same one.

The agent-facing text (skill, tool descriptions, server instructions) does not change.

## Measurement

Qwen3-Embedding 8B, 4B and 0.6B at 1024 dimensions (llama.cpp, last-token pooling), 646 answerable queries from the sixteen domain fixtures, pooled. With the generic instruction against none, pooled MRR rises on all three: 8B 0.826 to 0.878 (+0.051), 4B 0.853 to 0.888 (+0.035), 0.6B 0.865 to 0.887 (+0.023). Weighted 70/20/10 across the three it is 0.835 to 0.881 (+0.046). Gains are uneven across domains: on 8B six of sixteen get slightly worse (the worst by 0.037), and legal compliance gains 0.187. At 0.45 in single-domain pools, the share of off-topic searches left empty rises from 4% to 55% on 8B while 98% of answers are kept (99% before). The tables, the per-domain figures and the setup are in [docs/retrieval-experiments.md](../retrieval-experiments.md#query-instruction-adr-0097).

## Consequences

- An upgrade changes search ranking for existing deployments with no config change. Stored data is untouched: no re-embedding, and an empty `HIVEMIND_QUERY_INSTRUCTION` restores the old ranking at once.
- A deployment that sets `HIVEMIND_VECTOR_MIN_SIMILARITY` should know that answer similarity drops by about 0.01 to 0.03 at the 10th percentile. The recommended 0.45 still keeps 98% of answers.
- Statement-shaped queries, such as one that quotes an entry, rank that entry a little lower: on 8B, old-exact MRR in `make measure-versions` drops by about 0.04 and hit@5 barely moves. Version grouping itself compares entries and does not change.
- A deployment with a non-Qwen embedder should measure, and turn the instruction off if it does not help.
- The instruction is part of the query text the embedder sees, so it counts against the embedder's input limit (at most 500 characters more than the 2,000-character query bound).
