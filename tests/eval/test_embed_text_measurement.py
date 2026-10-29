"""The embedded-text measurement (``tests/eval/embed_text.py``, ROADMAP
§4.2(a)) runs.

On the 4-dimension hash embedder its numbers mean nothing (the entry text is
measured with a real model: ``make measure-embed-text``), so this pins the
machinery only: the variants it runs, the variant text it builds, the metric
ranges, the report and the JSON.
"""

from __future__ import annotations

import json

import pytest

from hivemind.domain.entry import EntryDraft, Kind
from tests.eval.domains import DOMAINS
from tests.eval.embed_text import (
    BASELINE,
    VARIANTS,
    VariantEmbedder,
    all_variants,
    extra_variants,
    measure,
    report,
    to_json,
    variant_text,
)
from tests.eval.threshold import domain_fixture
from tests.fakes import make_embedder


def test_variants_are_baseline_kind_tags_and_both() -> None:
    assert VARIANTS == ("baseline", "+kind", "+tags", "+kind+tags")
    assert all_variants() == VARIANTS
    assert all_variants(["custom", BASELINE]) == (*VARIANTS, "custom")


def test_extras_come_from_a_json_list() -> None:
    assert extra_variants(None) == []
    assert extra_variants("  ") == []
    assert extra_variants('["a", "b"]') == ["a", "b"]
    with pytest.raises(ValueError, match="JSON list"):
        extra_variants('{"a": 1}')
    with pytest.raises(ValueError, match="JSON list"):
        extra_variants('["a", 1]')


def test_variant_text_adds_only_the_named_fields() -> None:
    kind, summary, tags, body = "insight", "the summary", ("a", "b"), "the body"
    base = variant_text(kind, summary, tags, body, BASELINE)
    assert base == f"{summary}\n{body}"
    assert variant_text(kind, summary, tags, body, "+kind") == f"{summary}\nkind: insight\n{body}"
    assert variant_text(kind, summary, tags, body, "+tags") == f"{summary}\ntags: a, b\n{body}"
    assert variant_text(kind, summary, tags, body, "+kind+tags") == (
        f"{summary}\nkind: insight\ntags: a, b\n{body}"
    )
    # No body: the fields still land in the summary.
    assert variant_text(kind, summary, tags, None, "+tags") == f"{summary}\ntags: a, b"
    # An unknown label falls back to the baseline text.
    assert variant_text(kind, summary, tags, body, "custom") == base


async def test_the_variant_embedder_embeds_the_variant_text() -> None:
    embedder = make_embedder()
    draft = EntryDraft(
        kind=Kind("insight"),
        summary="the summary",
        tags=("a", "b"),
        body="the body",
        author="eval",
        agent="eval",
    )
    base = VariantEmbedder(embedder, BASELINE)
    tagged = VariantEmbedder(embedder, "+tags")
    assert tagged.model_name == embedder.model_name
    assert tagged.dimension == embedder.dimension
    # The entry text differs; a query embeds the same under both.
    assert tagged.entry_embeddable_text(draft) != base.entry_embeddable_text(draft)
    assert await tagged.embed_text("q") == await base.embed_text("q")
    assert await tagged.embed_entry(draft) != await base.embed_entry(draft)


async def test_every_variant_is_measured_and_the_report_reads() -> None:
    variants = all_variants(["custom"])
    rows = await measure(make_embedder(), domain_fixture(DOMAINS[4]), variants)
    assert [r.variant for r in rows] == list(variants)
    for r in rows:
        assert 0.0 <= r.mrr <= 1.0 and 0.0 <= r.hit_at_1 <= r.hit_at_5 <= 1.0
    text = report(rows, "hash")
    assert "baseline" in text and "`+tags`" in text and "change vs baseline" in text
    assert len(json.loads(to_json(rows, "hash"))["rows"]) == len(rows)
