"""The composition root every runner shares (``services/wiring.py``)."""

from __future__ import annotations

import logging

import pytest

from hivemind.config import Settings
from hivemind.domain.entry import EntryDraft, Kind
from hivemind.mcp.app import McpHivemind, hive_search, hive_write
from hivemind.ports import Credential
from hivemind.services.governance import PermissionDenied
from hivemind.services.wiring import build_services, log_startup
from tests.fakes import (
    FakeExtractor,
    make_authenticator,
    make_embedder,
    make_search_config,
    make_store,
)

ADMIN = Credential(user_id="admin", is_admin=True, access_controlled=True)


async def test_services_share_one_store_and_embedder() -> None:
    store = make_store()
    services = build_services(store, make_embedder(), make_search_config())
    draft = EntryDraft(kind=Kind.FACT, summary="wiring shares the store", author="a", agent="a")
    written = await services.write_service.write(draft)
    hits = await services.search_service.search("wiring shares the store")
    assert [h.entry_id for h in hits] == [written.id]
    report = await services.metrics_service.usage_report()
    assert report["entries"]["total"] == 1


async def test_the_search_config_reaches_search_and_quality() -> None:
    config = make_search_config()
    services = build_services(make_store(), make_embedder(), config)
    assert services.search_config is config
    assert await services.governance_service.quality("no-such-entry") == pytest.approx(1.0)


async def test_extractor_is_optional_and_used_when_given() -> None:
    store = make_store()
    services = build_services(
        store, make_embedder(), make_search_config(), extractor=FakeExtractor()
    )
    draft = EntryDraft(kind=Kind.FACT, summary="Postgres powers Hivemind", author="a", agent="a")
    entry = await services.write_service.write(draft)
    assert entry.entities  # the fake extractor ran


async def test_without_an_authenticator_key_management_is_refused() -> None:
    services = build_services(make_store(), make_embedder(), make_search_config())
    with pytest.raises(PermissionDenied):
        await services.access_service.rotate_org_key(ADMIN)


async def test_with_an_authenticator_key_management_works() -> None:
    services = build_services(
        make_store(),
        make_embedder(),
        make_search_config(),
        authenticator=make_authenticator(),
    )
    assert await services.access_service.rotate_org_key(ADMIN)


async def test_mcp_app_from_services_binds_the_credential() -> None:
    store = make_store()
    services = build_services(store, make_embedder(), make_search_config())
    credential = Credential(user_id="dev", agent_id="hivemind-mcp")
    app = McpHivemind.from_services(store, services, credential=credential)
    assert app.credential is credential
    assert app.store is store
    assert app.write_service is services.write_service
    written = await hive_write(app, kind="fact", summary="bound to the dev credential")
    assert written["author"] == "dev"
    found = await hive_search(app, query="bound to the dev credential")
    assert found["count"] == 1


def test_log_startup_line_is_unchanged_and_redacted(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(embedding_endpoint="https://bob:hunter2@emb.example/v1")
    caplog.set_level(logging.INFO)
    log_startup(logging.getLogger("hivemind.test"), "hivemind-api", settings)
    assert caplog.records[-1].name == "hivemind.test"
    assert caplog.messages[-1] == (
        "starting hivemind-api: embedding_endpoint=https://emb.example/v1 "
        f"embedding_dim={settings.embedding_dim} extraction=off "
        f"pool_max_size={settings.pool_max_size}"
    )
