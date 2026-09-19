"""The golden eval set (ROADMAP §1.1): a deterministic corpus + golden queries.

This is the committed, realistic-but-fixed set the retrieval eval runs on:
a small corpus of entries with clear, distinct topics + a set of queries,
each with its expected-relevant entry (by corpus index). The corpus is
seeded deterministically (deterministic ids, no Postgres, no network)
on top of ``tests/fakes.py`` so the eval is reproducible — the CI gate
pins the retrieval metrics (hit@k / MRR / nDCG) so future retrieval
changes are *measured*, not vibes.

The golden queries are *clearly* related to their relevant entry (shared
keywords + concepts), so a healthy hybrid pipeline ranks the relevant
entry at the top; the gate watches for a regression that breaks that.
"""

from __future__ import annotations

# (kind, summary, tags) — a fixed, deterministic corpus (distinct topics).
GOLDEN_CORPUS: list[tuple[str, str, list[str]]] = [
    ("fact", "PostgreSQL connection pooling with PgBouncer in transaction mode", ["postgres", "database"]),
    ("fact", "API rate limiting: token bucket allows 100 requests per minute per client", ["api", "rate-limit"]),
    ("decision", "Use Reciprocal Rank Fusion to merge the keyword and vector search streams", ["search", "retrieval"]),
    ("fact", "JWT auth: access token 15 minutes, refresh token 7 days, rotated on use", ["auth", "security"]),
    ("insight", "A 1536-dim embedding outperforms 512 for semantic search recall", ["embeddings", "search"]),
    ("fact", "Deploy config: staging canaries 10% then 100% after 30 minutes", ["deploy", "ops"]),
    ("insight", "Recency decay: entries lose retrieval weight on a 30-day half-life", ["retrieval", "decay"]),
    ("fact", "MCP tools: hive_search returns compact hits without bodies", ["mcp", "tools"]),
]

# (query, [corpus indices of the relevant entries]) — the golden query set.
GOLDEN_QUERIES: list[tuple[str, list[int]]] = [
    ("database connection pooling pgbouncer", [0]),
    ("rate limit requests per minute token bucket", [1]),
    ("how to merge keyword and vector search results with rank fusion", [2]),
    ("jwt access token refresh rotation security", [3]),
    ("which embedding dimension is best for semantic search", [4]),
    ("canary deployment strategy staging", [5]),
    ("retrieval half life decay of old entries", [6]),
    ("mcp compact hits progressive disclosure", [7]),
]


def golden_queries() -> list[tuple[str, list[int]]]:
    """The golden query set (query -> corpus indices of the relevant entries)."""
    return list(GOLDEN_QUERIES)


def golden_corpus() -> list[tuple[str, str, list[str]]]:
    """The golden corpus (kind, summary, tags)."""
    return list(GOLDEN_CORPUS)