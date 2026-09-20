"""Unit tests for the dim-mismatch guard in ``hivemind.store.migrate``.

The embedding dimension is a deploy-time decision baked into the pool's
``vector(:dim)`` column (ADR 0005). When a pool already exists at a
*different* dimension than the configured one, ``migrate`` must fail
**loudly** with an actionable error — never silently no-op and then fail
with a confusing ``DataError`` (``expected 512 dimensions, not 1536``)
on the first write (ROADMAP 1.2 dogfood, friction #8; ADR 0015).

Seam under test: the pure ``dim_mismatch_message`` builder (no I/O),
plus the pinned defaults — we never assume a 1536-dim default; the
default dim is 1024 when ``HIVEMIND_EMBEDDING_DIM`` is unset (ADR 0015).
"""

from __future__ import annotations

import inspect

from hivemind.config import Settings
from hivemind.store.migrate import dim_mismatch_message, migrate


class TestDimMismatchMessage:
    """The pure message builder: a match is ``None``, a mismatch is
    actionable (both dims named + both remediation paths)."""

    def test_matching_dim_returns_none(self) -> None:
        assert dim_mismatch_message(1024, 1024) is None

    def test_mismatch_names_both_dims(self) -> None:
        msg = dim_mismatch_message(512, 1024)
        assert msg is not None
        assert "512" in msg  # the pool's dim
        assert "1024" in msg  # the configured dim

    def test_mismatch_offers_a_fresh_pool_remediation(self) -> None:
        # Remediation 1: reset the pool and re-migrate at the configured dim.
        msg = dim_mismatch_message(512, 1024)
        assert msg is not None
        assert "pg-reset" in msg

    def test_mismatch_offers_a_match_the_pool_remediation(self) -> None:
        # Remediation 2: set HIVEMIND_EMBEDDING_DIM to the pool's dim.
        msg = dim_mismatch_message(512, 1024)
        assert msg is not None
        assert "HIVEMIND_EMBEDDING_DIM" in msg
        assert "512" in msg  # the env var should be set to the pool's dim


class TestDimDefaults:
    """ADR 0015: we never assume a 1536-dim default (that is one
    provider's native dim); the default is 1024, which both
    OpenAI-compatible endpoints (the ``dimensions`` parameter) and
    self-hosted Matryoshka servers (vLLM) can serve."""

    def test_default_embedding_dim_is_1024_never_1536(self) -> None:
        # The *code* default — what HIVEMIND_EMBEDDING_DIM falls back to
        # when the env var is unset. A deploy/Makefile override (e.g. the
        # dev Makefile's 512 for fast local vLLM embedding) is a
        # deployment override, not the code default; so assert the
        # declared field default, not the runtime (env-resolved) value.
        assert Settings.model_fields["embedding_dim"].default == 1024

    def test_migrate_signature_default_dim_is_1024(self) -> None:
        # A bare ``hivemind-migrate`` / fixture call must not bake 1536.
        params = inspect.signature(migrate).parameters
        assert params["dim"].default == 1024
