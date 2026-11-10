"""Deploy-time values a migration reads at load time (ADR 0020), since yoyo exec's each ``.py``
migration when the chain is read and passes it no arguments. ``migrate`` sets ``embedding_dim``
just before ``read_migrations``; nothing else writes it. yoyo re-execs the module on every read."""

from __future__ import annotations

# ADR 0015: 1024, never 1536 (one provider's native dim); keep in sync with
# ``Settings.embedding_dim``.
DEFAULT_EMBEDDING_DIM = 1024

#: The pgvector column width the next-read chain is built against.
embedding_dim: int = DEFAULT_EMBEDDING_DIM
