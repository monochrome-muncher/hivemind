"""Redacting an entry (ADR 0125) on both adapters."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from tests.integration.store_contract.helpers import (
    NO_SUCH_ID,
    T0,
    WIDE_SINCE,
    WIDE_UNTIL,
    Adapter,
    _clean,
    _junk,
    _state,
    _two_fleets,
    draft,
    vector_ids,
    write,
)

from hivemind.domain.audit import ActorKind, AuditAction, AuditEvent, AuditFilters
from hivemind.domain.entry import (
    EntityKind,
    Entry,
    EntryDraft,
    EntryFilters,
    EntryState,
    ExtractedEntity,
    ImportanceSource,
    Kind,
    Source,
    SourceType,
)
from hivemind.domain.entry_restore import EntryRedacted
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.domain.redaction import REDACTED_SUMMARY, RedactRefused
from hivemind.ports import SupersedeConflict, UnknownEntry


async def _secret_entry(
    adapter: Adapter, fleet_id: str | None = None, see_also: tuple[str, ...] = ()
) -> Entry:
    """A memory that carries a canary in every free-text field."""
    store = adapter.store
    return await store.create_entry(
        replace(
            draft(
                "hunter2canary summary",
                body="the password is hunter2canary",
                tags=("hunter2canary-tag",),
                sources=(Source(SourceType.URL, "https://example.test/hunter2canary"),),
                scope="fleet" if fleet_id else "org",
                fleet_id=fleet_id,
                see_also=see_also,
            ),
            payload={"token": "hunter2canary"},
        ),
        adapter.vec(3),
        "test-model",
        entities=(ExtractedEntity("hunter2canary-host", EntityKind.SYSTEM),),
        entities_model="ner-1",
    )


async def test_redact_wipes_every_text_field_and_keeps_the_rest(adapter: Adapter) -> None:
    store = adapter.store
    other = await write(adapter, "an ordinary memory", hot=3)
    secret = await _secret_entry(adapter, see_also=(other.id,))  # a link made from its text
    pointer = await store.create_entry(
        draft("points at the secret", see_also=(secret.id,)), adapter.vec(4), "m"
    )
    assert await store.entry_links(secret.id, 10) == ([other.id], [pointer.id])
    linking = await store.create_entry(
        draft("hunter2canary links out", see_also=(other.id,)), adapter.vec(3), "m"
    )
    await store.record_feedback(
        Feedback(secret.id, "bob", "bob-agent", Verdict.WRONG, "it said hunter2canary", T0)
    )
    await store.record_feedback(Feedback(secret.id, "cy", "cy-agent", Verdict.HELPFUL, "ok", T0))
    await store.record_feedback(Feedback(secret.id, "di", "di-agent", Verdict.STALE, None, T0))
    await store.withdraw_entry(secret.id, "contains hunter2canary", by_user="alice")
    await store.withdraw_entry(linking.id, None, by_user="alice")

    out = await store.redact_entry(secret.id)

    assert (out.feedback_notes, out.flag_notes) == (2, 1)  # a missing note is not counted
    for got in (out.entry, await store.get_entry(secret.id)):
        assert got is not None
        assert got.state is EntryState.WITHDRAWN and got.redacted_at is not None
        assert got.summary == REDACTED_SUMMARY and got.withdrawn_reason == REDACTED_SUMMARY
        assert (got.body, got.payload, got.sources, got.tags) == (None, None, (), ())
        assert (got.entities, got.entities_model, got.embedding_model) == ((), None, None)
        assert "hunter2canary" not in repr(got)
        # What stays.
        for field in ("id", "kind", "author", "agent", "scope", "fleet_id", "occurred_at"):
            assert getattr(got, field) == getattr(secret, field)
        assert got.created_at == secret.created_at and got.importance == secret.importance
    # Not findable by keyword or by vector, even when inactive entries are asked for.
    everything = EntryFilters(include_inactive=True)
    assert await store.search_keyword("hunter2canary", everything, 10) == [linking.id]
    assert await store.search_keyword("redacted", everything, 10) == []
    assert await store.search_keyword("tag host", everything, 10) == []
    assert secret.id not in await vector_ids(store, adapter.vec(3, base=0.0), everything, 10)
    assert (
        await store.list_entries(
            EntryFilters(entities=("hunter2canary-host",), include_inactive=True)
        )
        == []
    )
    assert (
        await store.list_entries(EntryFilters(tags=("hunter2canary-tag",), include_inactive=True))
        == []
    )
    # Its own outgoing links went; links to it stay (those memories still learn it changed).
    assert await store.entry_links(secret.id, 10) == ([], [pointer.id])
    # The feedback is untouched, notes included: it is only counted.
    notes = {fb.user: fb.note for fb in await store.list_feedback(secret.id, 10)}
    assert notes == {"bob": "it said hunter2canary", "cy": "ok", "di": None}
    # A memory that was not redacted is untouched.
    untouched = await store.get_entry(other.id)
    assert untouched is not None and untouched.summary == "an ordinary memory"
    assert untouched.redacted_at is None


async def test_redact_refuses_what_it_should(adapter: Adapter) -> None:
    store = adapter.store
    active = await write(adapter, "still active")
    with pytest.raises(RedactRefused, match="withdraw it first"):
        await store.redact_entry(active.id)
    got = await store.get_entry(active.id)
    assert got is not None and got.summary == "still active" and got.redacted_at is None

    gone = await write(adapter, "withdrawn then redacted")
    await store.withdraw_entry(gone.id, None, by_user="alice")
    await store.redact_entry(gone.id)
    with pytest.raises(RedactRefused, match="already redacted"):
        await store.redact_entry(gone.id)

    with pytest.raises(UnknownEntry):
        await store.redact_entry(NO_SUCH_ID)
    with pytest.raises(UnknownEntry):
        await store.redact_entry("not-a-uuid")


async def test_a_superseded_memory_can_be_redacted_and_keeps_its_chain(adapter: Adapter) -> None:
    store = adapter.store
    old = await _secret_entry(adapter)
    newer = await store.create_entry(
        draft("the corrected version", supersedes=(old.id,)), adapter.vec(0), "m"
    )
    out = await store.redact_entry(old.id)

    got = await store.get_entry(old.id)
    assert got is not None and out.entry.redacted_at == got.redacted_at
    assert got.state is EntryState.SUPERSEDED and got.superseded_by == newer.id
    assert got.summary == REDACTED_SUMMARY and got.withdrawn_reason is None
    assert (got.body, got.payload, got.sources, got.tags, got.entities) == (None, None, (), (), ())
    assert "hunter2canary" not in repr(got)
    (pred,) = await store.list_predecessors([newer.id])
    assert pred.id == old.id and pred.summary == REDACTED_SUMMARY
    everything = EntryFilters(include_inactive=True)
    assert await store.search_keyword("hunter2canary", everything, 10) == []
    assert old.id not in await vector_ids(store, adapter.vec(3, base=0.0), everything, 10)
    with pytest.raises(RedactRefused, match="already redacted"):
        await store.redact_entry(old.id)


async def test_redact_counts_the_withdrawal_reasons_in_the_audit_log(adapter: Adapter) -> None:
    store = adapter.store
    entry = await write(adapter, "withdrawn twice over")
    other = await write(adapter, "another")
    await store.withdraw_entry(entry.id, "leaked", by_user="admin")

    def withdrawal(target: str, reason: str | None) -> AuditEvent:
        return AuditEvent(
            ActorKind.ADMIN_KEY,
            "admin:1",
            AuditAction.ENTRY_WITHDRAW,
            target,
            {"author": "alice", "reason": reason},
        )

    for target, reason in (
        (entry.id, "leaked"),
        (entry.id, "  "),
        (entry.id, None),
        (other.id, "x"),
    ):
        await store.record_audit(withdrawal(target, reason))
    assert (await store.redact_entry(entry.id)).withdraw_reasons == 1
    # The audit rows are not edited.
    rows = await store.list_audit(AuditFilters(action=AuditAction.ENTRY_WITHDRAW), 10)
    assert sorted(str(r.detail["reason"]) for r in rows if r.target == entry.id) == [
        "  ",
        "None",
        "leaked",
    ]


async def test_redact_counts_the_withdrawal_reasons_a_restore_copied(adapter: Adapter) -> None:
    # A restore copies the withdrawal reason into its audit row as
    # `previous_reason` (ADR 0124), so a redaction counts those rows too.
    store = adapter.store
    entry = await write(adapter, "withdrawn, restored, withdrawn again")
    await store.withdraw_entry(entry.id, "again", by_user="admin")

    def restore_row(previous_reason: str | None) -> AuditEvent:
        return AuditEvent(
            ActorKind.ADMIN_KEY,
            "admin:1",
            AuditAction.ENTRY_RESTORE,
            entry.id,
            {"author": "alice", "previous_reason": previous_reason, "reason": "fine after all"},
        )

    await store.record_audit(restore_row("holds sk-canary"))
    await store.record_audit(restore_row(None))
    await store.record_audit(restore_row(" "))
    await store.record_audit(
        AuditEvent(
            ActorKind.ADMIN_KEY,
            "admin:1",
            AuditAction.ENTRY_WITHDRAW,
            entry.id,
            {"author": "alice", "reason": "again"},
        )
    )
    assert (await store.redact_entry(entry.id)).withdraw_reasons == 2


async def test_cleanup_does_not_offer_a_redacted_superseded_memory_for_restore(
    adapter: Adapter,
) -> None:
    store = adapter.store
    good = await write(adapter, "good fact", author="good")
    junk = await _junk(adapter, "junk over good", supersedes=(good.id,))
    await store.redact_entry(good.id)
    scan = await store.scan_agent_cleanup("bad", WIDE_SINCE, WIDE_UNTIL)
    assert scan.restorable == () and scan.restorable_total == 0
    out = await _clean(adapter)
    assert out.restored == 0
    assert (await _state(adapter, junk.id)).state is EntryState.WITHDRAWN
    stub = await _state(adapter, good.id)
    assert stub.state is EntryState.SUPERSEDED and stub.redacted_at is not None


async def test_restore_refuses_a_redacted_memory_and_keeps_it_withdrawn(adapter: Adapter) -> None:
    store = adapter.store
    secret = await _secret_entry(adapter)
    await store.withdraw_entry(secret.id, "leak", by_user="alice")
    await store.redact_entry(secret.id)
    with pytest.raises(EntryRedacted, match="redacted"):
        await store.restore_entry(secret.id)
    got = await store.get_entry(secret.id)
    assert got is not None and got.state is EntryState.WITHDRAWN
    assert got.summary == REDACTED_SUMMARY and got.redacted_at is not None
    # Also a superseded one: the redaction is reported, not "not withdrawn".
    old = await write(adapter, "old version")
    await store.create_entry(draft("new version", supersedes=(old.id,)), adapter.vec(0), "m")
    await store.redact_entry(old.id)
    with pytest.raises(EntryRedacted):
        await store.restore_entry(old.id)
    # A memory that was not redacted still restores.
    plain = await write(adapter, "plain")
    await store.withdraw_entry(plain.id, None, by_user="alice")
    assert (await store.restore_entry(plain.id)).entry.state is EntryState.ACTIVE


async def test_a_redacted_memory_cannot_be_withdrawn_or_superseded_back(adapter: Adapter) -> None:
    store = adapter.store
    secret = await write(adapter, "to wipe")
    await store.withdraw_entry(secret.id, "leak", by_user="alice")
    await store.redact_entry(secret.id)
    with pytest.raises(ValueError):
        await store.withdraw_entry(secret.id, None, by_user="alice")
    with pytest.raises(SupersedeConflict):
        await store.create_entry(draft("replacement", supersedes=(secret.id,)), adapter.vec(0), "m")
    got = await store.get_entry(secret.id)
    assert got is not None and got.state is EntryState.WITHDRAWN


async def test_a_redacted_memory_keeps_its_place_in_the_supersession_chain(
    adapter: Adapter,
) -> None:
    store = adapter.store
    old = await write(adapter, "first version")
    head = await store.create_entry(
        draft("second version", supersedes=(old.id,)), adapter.vec(0), "m"
    )
    await store.withdraw_entry(head.id, "leak", by_user="alice")
    await store.redact_entry(head.id)
    (pred,) = await store.list_predecessors([head.id])
    assert pred.id == old.id and pred.superseded_by == head.id
    got = await store.get_entry(head.id)
    assert got is not None and got.summary == REDACTED_SUMMARY


async def test_agent_cleanup_undo_never_brings_a_redacted_memory_back(adapter: Adapter) -> None:
    store = adapter.store
    leaked = await _junk(adapter, "junk with a secret", body="hunter2canary")
    other = await _junk(adapter, "ordinary junk")
    out = await _clean(adapter)
    assert out.withdrawn == 2
    await store.redact_entry(leaked.id)

    undone = await store.undo_agent_cleanup(out.record.id)

    assert undone.reactivated == 1 and undone.reactivate_skipped == 1
    assert (await _state(adapter, other.id)).state is EntryState.ACTIVE
    stub = await _state(adapter, leaked.id)
    assert stub.state is EntryState.WITHDRAWN and stub.redacted_at is not None
    assert stub.summary == REDACTED_SUMMARY and stub.body is None


async def test_a_fleet_merge_and_its_undo_leave_a_redacted_memory_withdrawn(
    adapter: Adapter,
) -> None:
    store = adapter.store
    a, b = await _two_fleets(adapter)
    secret = await _secret_entry(adapter, fleet_id=a)
    await store.withdraw_entry(secret.id, None, by_user="alice")
    await store.redact_entry(secret.id)

    merged = await store.merge_fleets(a, target_fleet_id=b, merged_by="admin:1")
    moved = await store.get_entry(secret.id)
    assert moved is not None and moved.fleet_id == b and moved.redacted_at is not None
    await store.unmerge_fleet(merged.record.id)

    back = await store.get_entry(secret.id)
    assert back is not None and back.fleet_id == a
    assert back.state is EntryState.WITHDRAWN and back.summary == REDACTED_SUMMARY
    assert back.body is None and back.redacted_at == moved.redacted_at


async def test_supersede_and_withdraw_leave_the_content_fields_unchanged(
    adapter: Adapter,
) -> None:
    """Entries are immutable: only the state fields move."""

    async def put(summary: str) -> Entry:
        made = EntryDraft(
            kind=Kind.DECISION,
            summary=summary,
            author="alice",
            agent="claude-code",
            body="the long form",
            payload={"k": [1, 2], "nested": {"a": "b"}},
            sources=(Source(SourceType.URL, "https://example.test/doc"),),
            tags=("auth", "jwt"),
            occurred_at=T0 - timedelta(days=3),
            importance=5,
            importance_source=ImportanceSource.CALLER,
            scope="self",
        )
        entities = (ExtractedEntity("JWT", EntityKind.CONCEPT),)
        return await adapter.store.create_entry(made, adapter.vec(1), "test-model", entities)

    async def snapshot(entry: Entry) -> Entry:
        got = await adapter.store.get_entry(entry.id)
        assert got is not None
        return got

    old, doomed = await put("to supersede"), await put("to withdraw")
    old_before, doomed_before = await snapshot(old), await snapshot(doomed)
    assert old_before.entities and old_before.payload and old_before.sources

    new = await write(adapter, "replacement", supersedes=(old.id,))
    await adapter.store.withdraw_entry(doomed.id, "obsolete", by_user="alice")

    old_after, doomed_after = await snapshot(old), await snapshot(doomed)
    assert (old_after.state, old_after.superseded_by) == (EntryState.SUPERSEDED, new.id)
    assert doomed_after.state is EntryState.WITHDRAWN
    moved = {
        "state": EntryState.ACTIVE,
        "superseded_by": None,
        "withdrawn_reason": None,
        "withdrawn_at": None,
    }
    assert replace(old_after, **moved) == old_before
    assert replace(doomed_after, **moved) == doomed_before
