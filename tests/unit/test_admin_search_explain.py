"""``POST /v1/admin/search`` with ``explain`` (ADR 0137): each hit carries the parts of its score,
for the admin only; the data-plane search and the agent-facing hit stay as they were."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import httpx
import pytest

from hivemind.api.deps import create_app
from hivemind.api.main import create_app_for_config
from hivemind.config import Settings
from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.recency import RecencyHint
from hivemind.embeddings import EmbeddingError
from hivemind.memstore import MemoryStore
from tests.fakes import FIXED_NOW, FakeAuthenticator, make_clock, make_embedder
from tests.fakes import make_search_config as search_config

ADMIN = {"X-API-Key": "hm_admin"}
QUERY = "kaniko cache"


class DownEmbedder:
    dimension = 4
    model_name = "down"

    async def embed_text(self, text: str) -> list[float]:
        raise EmbeddingError("embedder down")


class World:
    def __init__(self, embedder: object | None = None, social_max: float = 1.2) -> None:
        self.store = MemoryStore(make_clock())
        self.auth = FakeAuthenticator(store=self.store)
        self.app = create_app_for_config(
            Settings(),
            store=self.store,
            embedder=embedder or make_embedder(),  # type: ignore[arg-type]
            authenticator=self.auth,
            search_config=replace(search_config(), social_max=social_max),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(self.app)), base_url="http://t"
        )

    async def entry(self, summary: str, *, age_days: int = 0, importance: int = 3, author="bob"):  # type: ignore[no-untyped-def]
        draft = EntryDraft(
            kind=Kind.FACT,
            summary=summary,
            author=author,
            agent=author,
            scope="org",
            occurred_at=FIXED_NOW - timedelta(days=age_days),
            importance=importance,
            importance_source="caller" if importance != 3 else "default",  # type: ignore[arg-type]
        )
        return await self.store.create_entry(draft, await make_embedder().embed_text(summary))

    async def rate(self, entry_id: str, verdict: Verdict, count: int) -> None:
        for i in range(count):
            await self.store.record_feedback(
                Feedback(entry_id, f"rater{i}", "a", verdict, updated_at=FIXED_NOW)
            )

    async def search(self, path: str = "/v1/admin/search", headers=ADMIN, **body):  # type: ignore[no-untyped-def]
        return await self.client.post(path, json={"query": QUERY, **body}, headers=headers)


@pytest.fixture
async def w() -> World:
    world = World()
    fresh = await world.entry("kaniko cache setup in the build runner")
    await world.entry("kaniko cache warmup", age_days=400, importance=5)
    helped = await world.entry("kaniko cache layers explained", age_days=2)
    await world.rate(helped.id, Verdict.HELPFUL, 12)
    wrong = await world.entry("kaniko cache flags guide")
    await world.rate(wrong.id, Verdict.WRONG, 2)
    assert fresh
    return world


async def test_explain_is_absent_by_default(w: World) -> None:
    hits = (await w.search()).json()
    assert hits and all("explain" not in h for h in hits)
    assert all("explain" not in h for h in (await w.search(explain=False)).json())


async def test_an_ordinary_admin_search_is_byte_for_byte_the_same_with_explain_off(
    w: World,
) -> None:
    plain = await w.search()
    explained = await w.search(explain=True)
    stripped = [{k: v for k, v in h.items() if k != "explain"} for h in explained.json()]
    assert stripped == plain.json()


async def test_each_hit_explains_its_score_and_the_parts_multiply_to_it(w: World) -> None:
    hits = (await w.search(explain=True)).json()
    assert len(hits) == 4
    for hit in hits:
        e = hit["explain"]
        product = (
            e["fused"] * e["importance"]["factor"] * e["recency"]["factor"] * e["social"]["factor"]
        )
        assert hit["score"] == pytest.approx(product, rel=1e-3)
        assert e["score"] == hit["score"]
        assert e["mode"] == "hybrid"


async def test_the_parts_say_what_the_pipeline_did(w: World) -> None:
    by_summary = {h["summary"]: h["explain"] for h in (await w.search(explain=True)).json()}
    old = by_summary["kaniko cache warmup"]
    assert old["importance"] == {"value": 5, "factor": 1.1}
    assert old["recency"]["floor_applied"] is True
    assert old["recency"]["factor"] == 0.8
    assert old["recency"]["decay"] < 0.8
    helped = by_summary["kaniko cache layers explained"]
    assert helped["feedback"]["helpful"] == 12
    assert helped["feedback"]["quality"] == pytest.approx(1 + 12 * 0.2 / 14, rel=1e-4)
    assert helped["social"]["cap"] == 1.2 and helped["social"]["cap_applied"] is False
    assert helped["feedback"]["flags_halved"] is False  # nothing to halve without flags
    flagged = by_summary["kaniko cache flags guide"]
    assert flagged["feedback"]["wrong"] == 2 and flagged["social"]["factor"] < 1.0
    for e in by_summary.values():
        assert e["recency"]["hint"] == "default"
        assert e["keyword_rank"] is not None or e["vector_rank"] is not None


async def test_the_social_cap_is_reported_when_it_bound() -> None:
    world = World(social_max=1.0)
    entry = await world.entry("kaniko cache champion")
    await world.rate(entry.id, Verdict.HELPFUL, 200)
    social = (await world.search(explain=True)).json()[0]["explain"]["social"]
    assert social["cap_applied"] is True and social["factor"] == 1.0


async def test_a_recency_hint_is_named_in_the_parts(w: World) -> None:
    e = (await w.search(explain=True, recency="ignore_age")).json()[0]["explain"]
    assert e["recency"]["hint"] == "ignore_age"
    assert e["recency"]["half_life_days"] is None
    assert e["recency"]["factor"] == 1.0


async def test_a_keyword_only_answer_says_so() -> None:
    world = World(embedder=DownEmbedder())
    await world.entry("kaniko cache setup")
    resp = await world.search(explain=True)
    explain = resp.json()[0]["explain"]
    assert resp.headers["X-Hivemind-Degraded"] == "keyword-only"
    assert explain["mode"] == "keyword_only"
    assert explain["vector_rank"] is None and explain["keyword_rank"] == 1


async def test_explain_works_together_with_as_agent(w: World) -> None:
    await w.store.register_agent("amy")
    await w.store.activate_agent("amy", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=None)
    resp = await w.search(explain=True, as_agent="amy")
    assert resp.status_code == 200
    assert resp.json() and all("explain" in h for h in resp.json())


async def test_the_ranking_does_not_change_with_explain(w: World) -> None:
    plain = [(h["entry_id"], h["score"]) for h in (await w.search()).json()]
    explained = [(h["entry_id"], h["score"]) for h in (await w.search(explain=True)).json()]
    assert plain == explained


async def test_the_data_plane_search_ignores_explain_and_keeps_its_shape(w: World) -> None:
    await w.store.register_agent("amy")
    await w.store.activate_agent("amy", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=None)
    key = await w.auth.issue_agent_key("amy")
    plain = await w.search("/v1/search", {"X-API-Key": key})
    asked = await w.search("/v1/search", {"X-API-Key": key}, explain=True)
    assert asked.content == plain.content
    assert all("explain" not in h for h in asked.json())


async def test_an_agent_key_cannot_explain_through_the_admin_route(w: World) -> None:
    await w.store.register_agent("amy")
    await w.store.activate_agent("amy", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=None)
    key = await w.auth.issue_agent_key("amy")
    resp = await w.search(headers={"X-API-Key": key}, explain=True)
    assert resp.status_code == 403


def test_the_agent_facing_hit_shapes_never_carry_the_explanation() -> None:
    from dataclasses import replace as with_fields

    from hivemind.api.schemas import HitOut
    from hivemind.retrieval.scoring import score_parts
    from hivemind.services.search import Explanation
    from hivemind.wire import hit_dict
    from tests.unit.test_wire_contract import HIT

    parts = score_parts(0.02, 3, FIXED_NOW, 1.0, FIXED_NOW, 30.0, narrow_importance=True)
    explained = with_fields(
        HIT,
        explain=Explanation(parts, 1, 2, (0, 0, 0), False, RecencyHint.DEFAULT, False),
    )
    assert "explain" not in HitOut.model_fields
    assert HitOut.from_hit(explained).model_dump() == HitOut.from_hit(HIT).model_dump()
    assert hit_dict(explained) == hit_dict(HIT)


async def test_the_service_parts_reproduce_every_hits_score_exactly(w: World) -> None:
    hits = await w.app.search_service.search_result(QUERY, explain=True)
    assert len(hits.hits) == 4
    for hit in hits.hits:
        assert hit.explain is not None
        p = hit.explain.parts
        assert (
            hit.score
            == p.score
            == p.fused * p.importance_factor * p.recency_factor * p.social_factor
        )


async def test_the_service_computes_no_explanation_unless_asked(w: World) -> None:
    hits = await w.app.search_service.search_result(QUERY)
    assert all(hit.explain is None for hit in hits.hits)
