"""Unit tests for the dev stdio server's local embedder (ADR 0021).

The prefix budget must reach the embedded text: a ``LocalEmbedder``
built with a small ``prefix_tokens`` must truncate the body exactly as
the configured production embedder would (the bug ADR 0021 found was a
knob that never reached the embedder — the dev runner is no longer
exempt from it).
"""

from __future__ import annotations

from hivemind.domain.entry import EntryDraft, Kind
from hivemind.mcp.local_embedder import LocalEmbedder


def _draft(body: str) -> EntryDraft:
    return EntryDraft(kind=Kind.FACT, summary="summary", body=body, author="alice", agent="agent-1")


def test_the_default_prefix_budget_is_the_spec_default() -> None:
    embedder = LocalEmbedder(dimension=16)
    assert embedder.entry_embeddable_text(_draft("a b c d e f")) == "summary\na b c d e f"


def test_a_configured_prefix_budget_truncates_the_body() -> None:
    """Two body words past the 3-word budget are dropped; the summary
    stays whole (SPEC §7: the bound is on the body prefix only)."""
    embedder = LocalEmbedder(dimension=16, prefix_tokens=3)
    assert embedder.entry_embeddable_text(_draft("one two three four five")) == "summary\none two three"


def test_the_vector_reflects_the_truncation() -> None:
    """The truncation changes what is embedded: with and without the
    budget the vectors differ (a knob that never reached the text would
    make them identical)."""
    body = "alpha beta gamma delta epsilon"
    full = LocalEmbedder(dimension=32)
    bounded = LocalEmbedder(dimension=32, prefix_tokens=2)
    assert full.embed_entry(_draft(body)) != bounded.embed_entry(_draft(body))
