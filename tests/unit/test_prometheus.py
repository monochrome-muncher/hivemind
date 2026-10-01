"""``GET /metrics``: the Prometheus scrape target of the REST runner (ADR 0050)."""

from __future__ import annotations

from prometheus_client.parser import text_string_to_metric_families

from hivemind.api.prometheus import PrometheusMetrics
from hivemind.domain.entry import EntryDraft
from tests.unit.test_api_endpoints import make_client, make_hivemind_app, post_entry


def samples(text: str) -> dict[tuple[str, tuple[tuple[str, str], ...]], float]:
    """Every sample of a scrape, keyed by (name, sorted labels)."""
    return {
        (s.name, tuple(sorted(s.labels.items()))): s.value
        for family in text_string_to_metric_families(text)
        for s in family.samples
    }


async def test_metrics_is_prometheus_text_without_a_key() -> None:
    client = make_client(make_hivemind_app())
    async with client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert samples(resp.text)[("hivemind_usage_report_success", ())] == 1.0


async def test_metrics_reports_the_usage_counters_as_gauges() -> None:
    app = make_hivemind_app()
    store = app.store
    fleet = await store.create_fleet("data-eng")
    await store.create_entry(
        EntryDraft(
            kind="fact", summary="s1", author="alice", agent="a1", scope="fleet", fleet_id=fleet.id
        ),
        [0.1, 0.1, 0.1, 0.1],
    )
    await store.create_entry(
        EntryDraft(kind="insight", summary="s2", author="bob", agent="b1", scope="org"),
        [0.1, 0.1, 0.1, 0.1],
    )
    await store.register_agent("alice")
    client = make_client(app)
    async with client:
        got = samples((await client.get("/metrics")).text)
    assert got[("hivemind_entries", (("state", "active"),))] == 2
    assert got[("hivemind_entries", (("state", "inactive"),))] == 0
    assert got[("hivemind_entries_by_scope", (("scope", "fleet"),))] == 1
    assert got[("hivemind_entries_by_kind", (("kind", "insight"),))] == 1
    assert got[("hivemind_entries_by_author_kind", (("author", "alice"), ("kind", "fact")))] == 1
    assert got[("hivemind_fleets", ())] == 1
    assert got[("hivemind_fleet_entries", (("fleet", "data-eng"),))] == 1
    assert got[("hivemind_agents", (("status", "pending"),))] == 1
    assert got[("hivemind_agents_by_trust_level", (("trust_level", "untrusted"),))] == 1


async def test_requests_are_counted_by_route_template_and_status() -> None:
    client = make_client(make_hivemind_app())
    async with client:
        entry = await post_entry(client, "key-alice", "gamma rollout note", agent="a")
        await client.get(f"/v1/entries/{entry['id']}", headers={"X-API-Key": "key-alice"})
        await client.get("/v1/entries/x", headers={"X-API-Key": "nope"})
        await client.get("/no-such-path")
        got = samples((await client.get("/metrics")).text)

    def count(method: str, route: str, status: str) -> float:
        labels = (("method", method), ("route", route), ("status", status))
        return got.get(("hivemind_http_requests_total", labels), 0.0)

    assert count("POST", "/v1/entries", "201") == 1
    assert count("GET", "/v1/entries/{entry_id}", "200") == 1
    assert count("GET", "/v1/entries/{entry_id}", "401") == 1
    assert count("GET", "unmatched", "404") == 1
    # The raw id never becomes a label value.
    assert not any(entry["id"] in str(labels) for _, labels in got)
    latency = (
        "hivemind_http_request_duration_seconds_count",
        (("method", "POST"), ("route", "/v1/entries")),
    )
    assert got[latency] == 1


async def test_degraded_searches_are_counted() -> None:
    from hivemind.embeddings import EmbeddingError

    app = make_hivemind_app()
    client = make_client(app)
    async with client:
        await post_entry(client, "key-alice", "gamma rollout note", agent="a")

        async def boom(text: str) -> list[float]:
            raise EmbeddingError("embedding endpoint is down")

        app.search_service._embedder.embed_text = boom  # type: ignore[attr-defined]
        for _ in range(2):
            await client.post(
                "/v1/search", json={"query": "gamma"}, headers={"X-API-Key": "key-alice"}
            )
        got = samples((await client.get("/metrics")).text)
    assert got[("hivemind_search_degraded_total", ())] == 2


async def test_usage_report_is_cached_and_a_failure_keeps_the_last_snapshot() -> None:
    now = [0.0]
    calls = [0]
    fail = [False]

    async def report() -> dict[str, dict[str, object]]:
        calls[0] += 1
        if fail[0]:
            raise TimeoutError
        return {
            "entries": {
                "active": calls[0],
                "inactive": 0,
                "by_scope": {},
                "by_kind": {},
                "by_importance_source": {},
                "by_author_kind": {},
            },
            "fleets": {"total": 0, "writes_by_fleet": {}},
            "agents": {"pending": 0, "active": 0, "revoked": 0, "by_trust_level": {}},
        }

    metrics = PrometheusMetrics(report, ttl=30.0, clock=lambda: now[0])
    await metrics.refresh_usage()
    now[0] = 10.0
    await metrics.refresh_usage()
    assert calls[0] == 1  # within the TTL: no second scan

    now[0] = 31.0
    fail[0] = True
    await metrics.refresh_usage()
    assert calls[0] == 2
    assert metrics.usage_ok is False
    assert metrics.usage_snapshot is not None
    assert metrics.usage_snapshot["entries"]["active"] == 1
