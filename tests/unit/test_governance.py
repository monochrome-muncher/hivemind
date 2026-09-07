"""Unit tests for the write and governance services (SPEC.md §4.1, §8).

Seams under test: ``WriteService`` (embed-then-persist) and
``GovernanceService`` (withdrawal authorization, feedback upsert).
"""

from __future__ import annotations

import pytest

from hivemind.domain.entry import EntryDraft, Kind
from hivemind.domain.feedback import Verdict
from hivemind.memstore import MemoryStore
from hivemind.ports import Credential
from hivemind.services.governance import (
    GovernanceService,
    PermissionDenied,
    WriteService,
)
from tests.fakes import make_clock, make_embedder


def make_draft(summary: str, **kwargs) -> EntryDraft:
    return EntryDraft(
        kind=kwargs.pop("kind", Kind.FACT),
        summary=summary,
        author=kwargs.pop("author", "alice"),
        agent=kwargs.pop("agent", "agent-1"),
        **kwargs,
    )


class TestWriteService:
    async def test_write_embeds_then_persists(self) -> None:
        store = MemoryStore(make_clock())
        embedder = make_embedder()
        service = WriteService(store, embedder)

        entry = await service.write(make_draft("Auth uses JWT"))
        assert entry.embedding is not None
        assert len(entry.embedding) == embedder.dimension
        reloaded = await store.get_entry(entry.id)
        assert reloaded is not None
        assert reloaded.summary == "Auth uses JWT"

    async def test_write_with_supersedes_flips_targets(self) -> None:
        store = MemoryStore(make_clock())
        service = WriteService(store, make_embedder())

        old = await service.write(make_draft("v1"))
        new = await service.write(make_draft("v2", supersedes=(old.id,)))
        reloaded_old = await store.get_entry(old.id)
        reloaded_new = await store.get_entry(new.id)
        assert reloaded_old is not None
        assert reloaded_old.state.value == "superseded"
        assert reloaded_old.superseded_by == new.id
        assert reloaded_new is not None
        assert reloaded_new.state.value == "active"


class TestGovernanceService:
    @pytest.fixture
    def store(self) -> MemoryStore:
        return MemoryStore(make_clock())

    @pytest.fixture
    def governance(self, store: MemoryStore) -> GovernanceService:
        return GovernanceService(store)

    async def test_author_can_withdraw_own_entry(
        self, store: MemoryStore, governance: GovernanceService
    ) -> None:
        entry = await store.create_entry(make_draft("mine"))
        withdrawn = await governance.withdraw(
            Credential(user_id="alice", agent_id="agent-1"),
            entry.id,
            "no longer valid",
        )
        assert withdrawn.state.value == "withdrawn"
        assert withdrawn.withdrawn_reason == "no longer valid"

    async def test_non_author_cannot_withdraw(
        self, store: MemoryStore, governance: GovernanceService
    ) -> None:
        entry = await store.create_entry(make_draft("alice's", author="alice"))
        with pytest.raises(PermissionDenied):
            await governance.withdraw(Credential(user_id="bob", agent_id="agent-2"), entry.id)

    async def test_admin_can_withdraw_any_entry(
        self, store: MemoryStore, governance: GovernanceService
    ) -> None:
        entry = await store.create_entry(make_draft("alice's", author="alice"))
        withdrawn = await governance.withdraw(
            Credential(user_id="ops", agent_id="admin-agent", is_admin=True),
            entry.id,
            "admin correction",
        )
        assert withdrawn.state.value == "withdrawn"

    async def test_withdraw_unknown_entry_raises(self, governance: GovernanceService) -> None:
        with pytest.raises(LookupError):
            await governance.withdraw(Credential(user_id="alice", agent_id="agent-1"), "nope")

    async def test_withdraw_of_inactive_entry_raises(
        self, store: MemoryStore, governance: GovernanceService
    ) -> None:
        entry = await store.create_entry(make_draft("once active"))
        await store.withdraw_entry(entry.id, None, "alice")
        with pytest.raises(ValueError):
            await governance.withdraw(Credential(user_id="alice", agent_id="agent-1"), entry.id)

    async def test_record_feedback_upserts_per_reporter(self) -> None:
        store = MemoryStore(make_clock())
        governance = GovernanceService(store)
        entry = await store.create_entry(make_draft("a fact"))
        cred = Credential(user_id="bob", agent_id="agent-9")

        await governance.record_feedback(cred, entry.id, Verdict.HELPFUL)
        await governance.record_feedback(cred, entry.id, Verdict.WRONG)
        assert await store.feedback_counts(entry.id) == (0, 0, 1)

        # A different reporter counts separately.
        other = Credential(user_id="carol", agent_id="agent-10")
        await governance.record_feedback(other, entry.id, Verdict.STALE)
        assert await store.feedback_counts(entry.id) == (0, 1, 1)

    async def test_record_feedback_unknown_entry_raises(
        self, governance: GovernanceService
    ) -> None:
        with pytest.raises(LookupError):
            await governance.record_feedback(
                Credential(user_id="bob", agent_id="agent-9"),
                "nope",
                Verdict.HELPFUL,
            )

    async def test_quality_reflects_feedback(self) -> None:
        store = MemoryStore(make_clock())
        governance = GovernanceService(store)
        entry = await store.create_entry(make_draft("a fact"))
        assert await governance.quality(entry.id) == pytest.approx(1.0)

        for user in ("u1", "u2", "u3"):
            await governance.record_feedback(
                Credential(user_id=user, agent_id=f"agent-{user}"),
                entry.id,
                Verdict.WRONG,
            )
        # 1.0 - 3*0.25 = 0.25 -> clamped to 0.5
        assert await governance.quality(entry.id) == pytest.approx(0.5)
