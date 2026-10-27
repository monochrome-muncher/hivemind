"""Agent cleanup on real Postgres (ADR 0111): the row locks against in-flight
writes and ratings, two cleanups of one agent at once, a lock that cannot be
had, the audit rows and the whole thing through the services. Skips when
Postgres is down, like the rest of the suite."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import asyncpg
import pytest
from tests.fakes import FakeAuthenticator
from tests.pg import ensure_migrated

from hivemind.config import Settings
from hivemind.domain.access import AgentStatus, TrustLevel
from hivemind.domain.agent_cleanup import AgentCleanupRefused, cleanup_reason
from hivemind.domain.audit import AuditAction, AuditFilters
from hivemind.domain.entry import EntryDraft, EntryState, Kind
from hivemind.domain.feedback import Feedback, Verdict
from hivemind.ports import Credential
from hivemind.services.access import AccessService
from hivemind.store import PgStore
from hivemind.store import pgstore as pgstore_module

TABLES = (
    "TRUNCATE audit_log, credentials, agents, fleets, entries, feedbacks, search_counts, "
    "entry_links, pins, fleet_merges, fleet_merge_entries, agent_cleanups"
)
SINCE = datetime(2020, 1, 1, tzinfo=UTC)
UNTIL = datetime(2100, 1, 1, tzinfo=UTC)
ADMIN = Credential(user_id="admin", is_admin=True, key_id="abcdef012345")


@pytest.fixture
async def pool():
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    conn = await asyncpg.connect(dsn)
    await conn.execute(TABLES)
    await conn.close()
    store = PgStore(dsn)
    try:
        yield dsn, store
    finally:
        await store.close()
        conn = await asyncpg.connect(dsn)
        await conn.execute(TABLES)
        await conn.close()


def _draft(summary: str, author: str = "bad", **kwargs: object) -> EntryDraft:
    return EntryDraft(
        kind=Kind.FACT,
        summary=summary,
        author=author,
        agent=author,
        scope="org",
        **kwargs,  # type: ignore[arg-type]
    )


async def _entry(store: PgStore, summary: str, author: str = "bad", **kwargs: object) -> str:
    return (await store.create_entry(_draft(summary, author, **kwargs), None)).id


async def _row(dsn: str, sql: str, *args: object):  # type: ignore[no-untyped-def]
    conn = await asyncpg.connect(dsn)
    try:
        return await conn.fetchrow(sql, *args)
    finally:
        await conn.close()


async def test_a_concurrent_supersede_of_a_junk_memory_is_not_overwritten(pool) -> None:
    """The cleanup waits for a writer that holds a junk memory's row, then reads
    what the writer left: a memory superseded in the meantime is no longer
    active, so it is not withdrawn (and its successor is not undone)."""
    dsn, store = pool
    keep = await _entry(store, "kept")
    taken = await _entry(store, "taken by a supersede")
    writer = await asyncpg.connect(dsn)
    tx = writer.transaction()
    await tx.start()
    successor = await writer.fetchval(
        "INSERT INTO entries (kind, summary, author, agent, scope, occurred_at) "
        "VALUES ('fact', 'better', 'good', 'good', 'org', now()) RETURNING id"
    )
    await writer.execute(
        "UPDATE entries SET state = 'superseded', superseded_by = $1 WHERE id = $2",
        successor,
        taken,
    )
    task = asyncio.create_task(store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1"))
    await asyncio.sleep(0.4)
    assert not task.done()  # waiting on the row lock
    await tx.commit()
    await writer.close()

    out = await task

    assert out.record is not None and out.record.withdrawn_ids == (keep,)
    row = await _row(dsn, "SELECT state, superseded_by FROM entries WHERE id = $1", taken)
    assert (row["state"], str(row["superseded_by"])) == ("superseded", str(successor))


async def test_an_agents_rating_in_flight_waits_and_then_counts_again(pool) -> None:
    dsn, store = pool
    target = await _entry(store, "target", "good")
    await store.record_feedback(Feedback(target, "bad", "a", Verdict.WRONG, "n"))
    rater = await asyncpg.connect(dsn)
    tx = rater.transaction()
    await tx.start()
    # The agent is re-rating the memory right now: it holds the feedback row.
    await rater.execute(
        "UPDATE feedbacks SET verdict = $2, updated_at = now() WHERE entry_id = $1 AND \"user\" = 'bad'",
        target,
        "stale",
    )
    task = asyncio.create_task(store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1"))
    await asyncio.sleep(0.4)
    assert not task.done()
    await tx.commit()
    await rater.close()
    out = await task
    assert out.ratings_set_aside == 1  # it set aside the newest version, not a stale copy
    assert await store.feedback_counts(target) == (0, 0, 0)
    row = await _row(dsn, "SELECT verdict FROM feedbacks WHERE \"user\" = 'bad'")
    assert row["verdict"] == "stale"


async def test_a_rating_after_the_cleanup_clears_the_mark_in_the_database(pool) -> None:
    dsn, store = pool
    target = await _entry(store, "target", "good")
    await store.record_feedback(Feedback(target, "bad", "a", Verdict.WRONG, "n"))
    out = await store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1")
    assert out.record is not None
    marked = await _row(dsn, "SELECT set_aside_by FROM feedbacks")
    assert str(marked["set_aside_by"]) == out.record.id
    await store.record_feedback(Feedback(target, "bad", "a", Verdict.STALE, "again"))
    assert (await _row(dsn, "SELECT set_aside_by FROM feedbacks"))["set_aside_by"] is None
    assert await store.feedback_counts(target) == (0, 1, 0)


async def test_two_cleanups_of_one_agent_at_once_never_withdraw_a_memory_twice(pool) -> None:
    _, store = pool
    ids = {await _entry(store, f"junk {i}") for i in range(30)}
    first, second = await asyncio.gather(
        store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1"),
        store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:2"),
    )
    records = [o.record for o in (first, second) if o.record is not None]
    assert len(records) == 1  # the second one found nothing left
    assert set(records[0].withdrawn_ids) == ids
    assert len(await store.list_agent_cleanups(10)) == 1


async def test_cleanups_of_different_agents_do_not_wait_on_each_other(pool) -> None:
    _, store = pool
    await _entry(store, "bad junk", "bad")
    await _entry(store, "worse junk", "worse")
    first, second = await asyncio.gather(
        store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1"),
        store.cleanup_agent("worse", SINCE, UNTIL, cleaned_by="admin:1"),
    )
    assert (first.withdrawn, second.withdrawn) == (1, 1)


async def test_the_agent_writing_while_it_is_cleaned_never_gets_a_half_state(pool) -> None:
    """Writers keep writing while a cleanup runs. Every memory ends up either
    withdrawn by that cleanup (in its record) or active (written after its
    read), and an undo brings back exactly the recorded ones."""
    dsn, store = pool
    for i in range(20):
        await _entry(store, f"early {i}")
    writers = [asyncio.create_task(_entry(store, f"racing {i}")) for i in range(40)]
    await asyncio.sleep(0.01)
    out = await store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1")
    written = await asyncio.gather(*writers)
    assert out.record is not None
    row = await _row(dsn, "SELECT count(*) AS n FROM entries WHERE state = 'withdrawn'")
    assert row["n"] == out.withdrawn == len(out.record.withdrawn_ids)
    rows = await _row(
        dsn,
        "SELECT count(*) AS n FROM entries WHERE state = 'active' AND author = 'bad'",
    )
    assert rows["n"] + out.withdrawn == 20 + len(written)
    undone = await store.undo_agent_cleanup(out.record.id)
    assert undone.reactivated == out.withdrawn
    rows = await _row(dsn, "SELECT count(*) AS n FROM entries WHERE state = 'active'")
    assert rows["n"] == 20 + len(written)


async def test_a_cleanup_racing_the_agents_ratings_keeps_every_row(pool) -> None:
    dsn, store = pool
    targets = [await _entry(store, f"t{i}", "good") for i in range(20)]
    for t in targets:
        await store.record_feedback(Feedback(t, "bad", "a", Verdict.WRONG, "n"))

    async def rate_again(t: str) -> None:
        await store.record_feedback(Feedback(t, "bad", "a", Verdict.STALE, "again"))

    tasks = [asyncio.create_task(rate_again(t)) for t in targets]
    out = await store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1")
    await asyncio.gather(*tasks)
    assert out.record is not None
    row = await _row(dsn, "SELECT count(*) AS n, count(set_aside_by) AS marked FROM feedbacks")
    assert row["n"] == 20  # no row lost
    # A row is set aside only if the cleanup got to it after the agent re-rated it,
    # or it was cleared by the re-rating: never marked by something else.
    assert row["marked"] <= out.record.ratings_set_aside
    undone = await store.undo_agent_cleanup(out.record.id)
    assert undone.ratings_cleared == row["marked"]
    assert (await _row(dsn, "SELECT count(set_aside_by) AS n FROM feedbacks"))["n"] == 0


async def test_a_lock_that_cannot_be_had_is_refused_not_waited_for_forever(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    dsn, store = pool
    junk = await _entry(store, "junk")
    monkeypatch.setattr(
        pgstore_module,
        "MERGE_SETTINGS",
        "SET LOCAL lock_timeout = '300ms'; SET LOCAL statement_timeout = 0;",
    )
    holder = await asyncpg.connect(dsn)
    tx = holder.transaction()
    await tx.start()
    await holder.execute("SELECT 1 FROM entries WHERE id = $1 FOR UPDATE", junk)
    try:
        with pytest.raises(AgentCleanupRefused, match="try again"):
            await store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1")
    finally:
        await tx.rollback()
        await holder.close()
    row = await _row(dsn, "SELECT state FROM entries WHERE id = $1", junk)
    assert row["state"] == "active"  # nothing changed
    assert await store.list_agent_cleanups(10) == []
    out = await store.cleanup_agent(
        "bad", SINCE, UNTIL, cleaned_by="admin:1"
    )  # and it can be retried
    assert out.withdrawn == 1


async def test_undo_leaves_a_memory_whose_withdrawal_is_no_longer_this_cleanups(pool) -> None:
    dsn, store = pool
    one, two = await _entry(store, "junk one"), await _entry(store, "junk two")
    out = await store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1")
    assert out.record is not None
    conn = await asyncpg.connect(dsn)
    await conn.execute(
        "UPDATE entries SET withdrawn_reason = 'withdrawn again by hand' WHERE id = $1", one
    )
    await conn.close()
    undone = await store.undo_agent_cleanup(out.record.id)
    assert (undone.reactivated, undone.reactivate_skipped) == (1, 1)
    row = await _row(dsn, "SELECT state, withdrawn_reason FROM entries WHERE id = $1", one)
    assert (row["state"], row["withdrawn_reason"]) == ("withdrawn", "withdrawn again by hand")
    row = await _row(
        dsn, "SELECT state, withdrawn_reason, withdrawn_at FROM entries WHERE id = $1", two
    )
    assert (row["state"], row["withdrawn_reason"], row["withdrawn_at"]) == ("active", None, None)


async def test_the_cleanup_sets_withdrawn_at_and_the_reason_like_a_normal_withdraw(pool) -> None:
    dsn, store = pool
    junk = await _entry(store, "junk")
    out = await store.cleanup_agent("bad", SINCE, UNTIL, cleaned_by="admin:1")
    assert out.record is not None
    row = await _row(
        dsn, "SELECT state, withdrawn_reason, withdrawn_at FROM entries WHERE id = $1", junk
    )
    assert row["state"] == "withdrawn" and row["withdrawn_at"] is not None
    assert row["withdrawn_reason"] == cleanup_reason(out.record.id)


async def test_the_preview_cuts_its_lists_and_keeps_exact_totals(
    pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, store = pool
    monkeypatch.setattr(pgstore_module, "PREVIEW_WITHDRAW_LIMIT", 3)
    monkeypatch.setattr(pgstore_module, "PREVIEW_RESTORE_LIMIT", 2)
    kept = [await _entry(store, f"good {i}", "good") for i in range(4)]
    for i, k in enumerate(kept):
        await _entry(store, f"junk {i}", supersedes=(k,))
    await _entry(store, "junk 4")
    scan = await store.scan_agent_cleanup("bad", SINCE, UNTIL)
    assert (len(scan.withdraw), scan.withdraw_total) == (3, 5)
    assert [e.summary for e in scan.withdraw] == ["junk 4", "junk 3", "junk 2"]
    assert (len(scan.restorable), scan.restorable_total) == (2, 4)


async def test_through_the_services_with_the_revoke_and_the_audit_rows(pool) -> None:
    _, store = pool
    access = AccessService(store, FakeAuthenticator(store=store))
    fleet = await store.create_fleet("alpha")
    for name in ("bad", "good"):
        await store.register_agent(name)
        await store.activate_agent(name, trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id)
    good = await _entry(store, "good fact", "good")
    junk = (
        await store.create_entry(
            EntryDraft(
                kind=Kind.FACT,
                summary="pinned junk",
                author="bad",
                agent="bad",
                scope="fleet",
                fleet_id=fleet.id,
                supersedes=(good,),
            ),
            None,
        )
    ).id
    await store.pin_entry(fleet.id, junk, "bad", 10)
    await store.record_feedback(Feedback(good, "bad", "a", Verdict.WRONG, "n"))

    preview = await access.cleanups.preview("bad", ADMIN, since=SINCE, until=UNTIL)
    assert preview.scan.withdraw_total == 1 and preview.scan.ratings_total == 1
    out = await access.cleanups.cleanup(
        "bad",
        ADMIN,
        since=SINCE,
        until=UNTIL,
        restore_ids=[good],
        expected_withdraw=1,
        expected_ratings=1,
        also_revoke=True,
    )

    assert out.record is not None and out.revoked and out.record.revoked
    assert (await store.get_agent("bad")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    assert (await store.get_entry(good)).state is EntryState.ACTIVE  # type: ignore[union-attr]
    assert await store.list_pins(fleet.id) == []
    stored = await store.get_agent_cleanup(out.record.id)
    assert stored is not None and stored.revoked
    audits = await store.list_audit(AuditFilters(), 100)
    by_action = {a: [r for r in audits if r.action is a] for a in AuditAction}
    (cleanup_row,) = by_action[AuditAction.AGENT_CLEANUP]
    assert cleanup_row.target == "bad" and cleanup_row.detail["withdrawn"] == 1
    assert [r.target for r in by_action[AuditAction.AGENT_REVOKE]] == ["bad"]
    assert [r.detail["reason"] for r in by_action[AuditAction.ENTRY_UNPIN]] == ["agent_cleanup"]

    undone = await access.cleanups.undo(out.record.id, ADMIN)
    assert undone.reactivated == 1 and undone.resuperseded == 1 and undone.ratings_cleared == 1
    assert (await store.get_agent("bad")).status is AgentStatus.REVOKED  # type: ignore[union-attr]
    assert [
        r.target
        for r in await store.list_audit(AuditFilters(action=AuditAction.AGENT_CLEANUP_UNDO), 10)
    ] == ["bad"]
