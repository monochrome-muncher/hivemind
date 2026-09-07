# Fixed-dimension pgvector with operator-configured embeddings

Embeddings are generated at write time by an OpenAI-compatible endpoint that the organization configures at deploy (OpenAI, or a self-hosted vLLM/Ollama if data must not leave the org). The pgvector column is provisioned with a fixed dimension at deploy time (default 1536, matching `text-embedding-3-small`), and the stored embedding records the model name. Changing models later is an operator-run re-embedding migration (pgvector dimension is per-column, so a different-dimension model means a new column, backfill, and swap).

Considered options: per-model dynamic columns or a model-registry table; embedding on the client side. Rejected: dynamic multi-model columns add schema and query complexity v1 does not need; client-side embedding breaks the guarantee that every entry is searchable and makes provenance of embeddings unenforceable.

Consequences: the embedding model is a deploy-time lock-in decision; model changes are a deliberate operator migration, not a config flip.