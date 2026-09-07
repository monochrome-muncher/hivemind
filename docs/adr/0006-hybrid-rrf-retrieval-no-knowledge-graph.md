# Hybrid retrieval: RRF fusion of BM25 + dense, no knowledge graph in v1

v1 search runs BM25 (Postgres FTS) and dense (pgvector) in parallel, fuses the two ranked lists with Reciprocal Rank Fusion (weights configurable, default 0.5/0.5), then re-scores by importance × recency (decay from occurrence time) × feedback quality. No knowledge graph in v1.

Considered options: vector-only search; graph-expanded retrieval (Zep/Caura style, entity extraction + multi-hop expansion). Rejected for v1: graph extraction is a heavy, quality-sensitive layer whose payoff (cross-entry entity linking) is a documented extension, not a v1 requirement. RRF + decay-aware re-scoring is the pattern the field converged on (agentmemory, pgmemai, Caura) and is a small, well-understood implementation; the fusion weights and decay parameters are config knobs, so tuning is cheap.

Consequences: retrieval quality depends on the quality of the `summary` field (the embedded text) — the agent is instructed to write good summaries. Graph expansion and entity linking are v2+ extensions.