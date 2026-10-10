"""A revoked agent's feedback no longer counts (ADR 0067): through the admin
revoke path, an entry only that agent reported returns to neutral."""

from __future__ import annotations

from datetime import UTC, datetime

from hivemind.domain.access import TrustLevel
from hivemind.domain.entry import EntryDraft, EntryFilters, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from tests.fakes import FakeAuthenticator, make_store


async def test_revoking_the_only_stale_reporter_makes_the_entry_neutral() -> None:
    store = make_store()
    service = AccessService(store, FakeAuthenticator(store=store))
    admin = Credential(user_id="admin", is_admin=True)
    await service.register("scout", admin, owner_alias="john")
    await store.activate_agent("scout", trust_level=TrustLevel.LURKER, home_fleet_id=None)
    entry = await store.create_entry(
        EntryDraft(
            kind=Kind.FACT,
            summary="the deploy runs on friday",
            author="john",
            agent="claude-code",
            scope="org",
        ),
        [0.0] * 8,
        "test-model",
    )
    await store.record_feedback(
        Feedback(entry.id, "scout", "scout", Verdict.STALE, "moved", datetime.now(UTC))
    )
    assert await store.quality_counts([entry.id]) == {entry.id: (0, 1, 0)}
    assert [e.id for e in await store.list_entries(EntryFilters(flagged=True))] == [entry.id]

    await service.revoke("scout", admin)

    assert await store.quality_counts([entry.id]) == {entry.id: (0, 0, 0)}
    assert await store.list_entries(EntryFilters(flagged=True)) == []
    assert await store.list_feedback(entry.id, 10) == []
