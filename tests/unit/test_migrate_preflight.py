"""Pre-flight guards in ``hivemind.store.migrate`` (pure helpers, no I/O).

pgvector >= 0.8 (``hnsw.iterative_scan``) and an embedding dim <= 2000
(the HNSW limit) are hard requirements of migration ``0004`` / ``search``;
they must fail ``migrate`` with an actionable message, not a raw Postgres
error or a search that breaks at first use.
"""

from __future__ import annotations

from hivemind.store.migrate import (
    invalid_index_message,
    pgvector_requirement_message,
)


def test_supported_version_and_dim_pass() -> None:
    assert pgvector_requirement_message("0.8.0", 1024) is None
    assert pgvector_requirement_message("0.8.1", 2000) is None
    assert pgvector_requirement_message("0.10.0", 512) is None
    assert pgvector_requirement_message("1.0.0", 512) is None


def test_old_pgvector_is_rejected_with_the_remedy() -> None:
    msg = pgvector_requirement_message("0.7.4", 1024)
    assert msg is not None
    assert "0.7.4" in msg and "0.8" in msg
    assert "ALTER EXTENSION vector UPDATE" in msg


def test_dim_above_the_hnsw_limit_is_rejected() -> None:
    msg = pgvector_requirement_message("0.8.1", 3072)
    assert msg is not None
    assert "3072" in msg and "2000" in msg
    assert "HIVEMIND_EMBEDDING_DIM" in msg


def test_unparseable_version_is_not_a_false_alarm() -> None:
    assert pgvector_requirement_message("weird", 1024) is None


def test_invalid_index_message_names_the_runbook_remedy() -> None:
    assert invalid_index_message(True) is None
    msg = invalid_index_message(False)
    assert msg is not None
    assert "entries_embedding_hnsw_idx" in msg
    assert "REINDEX INDEX CONCURRENTLY" in msg
    assert "ops-runbook" in msg
