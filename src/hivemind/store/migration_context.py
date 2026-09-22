"""Deploy-time values a migration needs at *load* time (ADR 0020).

yoyo exec's each ``.py`` migration when the chain is read, so a
migration whose DDL is parameterised — the pgvector column width, a
deploy-time decision (ADR 0005) — cannot take it as an argument. It
reads it from here instead.

``migrate`` sets ``embedding_dim`` immediately before ``read_migrations``
and nothing else writes to it. A fresh ``Migration`` object is built on
every ``read_migrations`` call, so the module is re-exec'd each time and
picks up the current value (yoyo never caches the module in
``sys.modules``).
"""

from __future__ import annotations

# ADR 0015: the code default is 1024 — never 1536 (that is one
# provider's native dim). Kept in sync with ``Settings.embedding_dim``.
DEFAULT_EMBEDDING_DIM = 1024

#: The pgvector column width the next-read chain is built against.
embedding_dim: int = DEFAULT_EMBEDDING_DIM
