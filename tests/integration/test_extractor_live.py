"""Live end-to-end test for the extractor lane (ADR 0016, SPEC §13).

Runs the real ``OpenAICompatExtractor`` against a *live*
OpenAI-compatible chat endpoint (the ``HIVEMIND_EXTRACTOR_*`` env vars;
dev/test: ``http://localhost:8080/v1``, model ``qwen3.8-27b``, API key
``dummy``). Skips cleanly when extraction is off (the endpoint is
unset — ADR 0016's optional stance) or the endpoint is unreachable, so
``uv run pytest`` stays green on a machine without a chat service.

The facet *storage* seam (``entities`` / ``entity_names`` /
``entities_model`` round-trip + filter) is covered hermetically by
``tests/integration/test_pgstore_entities.py`` and
``tests/unit/test_entities.py`` — this module only proves the live
model returns output that passes the all-or-nothing validation.
"""

from __future__ import annotations

import httpx
import pytest

from hivemind.config import Settings
from hivemind.domain.entry import EntityKind, EntryDraft, Kind
from hivemind.extractor import build_extractor

# The extraction text is deliberately about two named systems so a sane
# model must return at least one entity for each.
LIVE_DRAFT = EntryDraft(
    kind=Kind.FACT,
    summary="Postgres connection pool exhausted during the vLLM rollout",
    author="alice",
    agent="claude-code",
    body=(
        "The Postgres connection pool ran out of connections while the vLLM "
        "embeddings server was scaling up. The auth-service retries masked "
        "the outage until the dashboards went red."
    ),
)


def _skip_if_extraction_off_or_unreachable() -> None:
    """Skip (not fail) when the extractor is off or the endpoint is down."""
    settings = Settings()
    if not settings.extractor_endpoint:
        pytest.skip(
            "HIVEMIND_EXTRACTOR_ENDPOINT unset — extraction is off "
            "(ADR 0016: optional; zero LLM-extraction cost)"
        )
    try:
        # Any HTTP response means the service is listening; only
        # connection-level failures count as "unreachable".
        with httpx.Client(timeout=3.0) as client:
            client.get(settings.extractor_endpoint.rstrip("/"))
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as exc:
        pytest.skip(f"extractor endpoint {settings.extractor_endpoint} unreachable: {exc}")


async def test_live_extractor_returns_validated_facets() -> None:
    """The live model's output must pass the all-or-nothing validation
    (a closed ``kind`` vocabulary, open-but-bounded names) — and for
    this draft, yield at least one entity."""
    _skip_if_extraction_off_or_unreachable()
    settings = Settings()
    extractor = build_extractor(settings)
    assert extractor is not None  # the endpoint is set (we just checked)

    entities = await extractor.extract_entry(LIVE_DRAFT)

    # A valid result: a tuple of schema-validated facets.
    assert isinstance(entities, tuple)
    assert len(entities) >= 1, "expected at least one entity for a Postgres/vLLM draft"
    assert len(entities) <= 10  # the SPEC §13.1 cap
    for entity in entities:
        assert isinstance(entity.name, str)
        assert entity.name.strip() == entity.name  # trimmed
        assert 0 < len(entity.name) <= 128  # non-empty, bounded
        assert isinstance(entity.kind, EntityKind)  # closed vocabulary
    # Names are deduped case-insensitively (SPEC §13.1).
    lowercased = {e.name.lower() for e in entities}
    assert len(lowercased) == len(entities)


async def test_live_extractor_satisfies_the_port() -> None:
    """The production extractor structurally satisfies the ``Extractor``
    port (``model_name`` + ``extract_entry``), so it is a drop-in for
    the ``WriteService`` hook."""
    _skip_if_extraction_off_or_unreachable()
    from hivemind.ports import Extractor

    extractor = build_extractor(Settings())
    assert extractor is not None
    assert isinstance(extractor, Extractor)
    assert isinstance(extractor.model_name, str)
    assert extractor.model_name  # a deploy-time decision must be configured
