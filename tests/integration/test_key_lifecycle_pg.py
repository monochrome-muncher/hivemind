"""Postgres-backed tests for the key-lifecycle invariants (ADRs 0028, 0031, 0039).

* a key authenticates as an agent only while ``agents.status = 'active'``
  (``verify`` joins ``agents``; one query);
* activate / revoke are status-guarded and atomic with the key: a racing
  pair can never leave a ``revoked`` agent holding a live key;
* one live key per agent and one live org key, enforced by unique
  partial indexes (migration ``0007``) plus serialisation;
* the CLI ``issue-agent`` cannot silently restore a revoked agent's stale
  trust or fleet, nor mint a second key for a pending one;
* pre-v2 ``user`` keys (and name-less ``agent`` keys) no longer verify.

Skips cleanly when Postgres is unreachable.
"""

from __future__ import annotations

import asyncio
import json

import asyncpg
import pytest
from tests.pg import ensure_migrated

from hivemind.config import Settings
from hivemind.domain.access import InvalidAgentStatus, TrustLevel
from hivemind.domain.audit import ActorKind
from hivemind.services.access import AccessService
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store.auth import key_hash
from hivemind.store.keys import (
    AgentKeyExists,
    AgentNeedsActivation,
    AgentNotActive,
    FleetMergedError,
    _issue_admin,
    _issue_agent,
    _rotate_agent,
    _rotate_org,
)
from hivemind.store.migrate import migrate

TABLES = "TRUNCATE audit_log, credentials, agents, fleets, entries, feedbacks, pins, fleet_merges, fleet_merge_entries"


async def _exec(dsn: str, sql: str, *args: object) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql, *args)
    finally:
        await conn.close()


async def _fetch(dsn: str, sql: str, *args: object) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        return list(await conn.fetch(sql, *args))
    finally:
        await conn.close()


@pytest.fixture
async def env():
    dsn = Settings().database_url
    await ensure_migrated(dsn, Settings().embedding_dim)
    await _exec(dsn, TABLES)
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    try:
        yield dsn, store, auth
    finally:
        await store.close()
        await auth.close()
        await _exec(dsn, TABLES)


async def _admin(dsn: str, auth: PgAuthenticator):
    credential = await auth.verify(await _issue_admin(dsn, actor="t"))
    assert credential is not None
    return credential


# -- AUTH-1: verify requires an active agent ----------------------------------


@pytest.mark.parametrize("status", ["pending", "revoked"])
async def test_verify_rejects_a_key_whose_agent_is_not_active(env, status) -> None:
    dsn, _, auth = env
    await _exec(dsn, "INSERT INTO agents (name, status, trust_level) VALUES ('bob', $1, 3)", status)
    await _exec(
        dsn,
        "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
        "VALUES ($1, 'agent', 'bob', 'bob', 'bob')",
        key_hash("hm_stale"),
    )
    assert await auth.verify("hm_stale") is None


async def test_verify_rejects_an_agent_key_with_no_agent_record(env) -> None:
    dsn, _, auth = env
    await _exec(
        dsn,
        "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
        "VALUES ($1, 'agent', 'ghost', 'ghost', 'ghost')",
        key_hash("hm_ghost"),
    )
    assert await auth.verify("hm_ghost") is None


async def test_verify_resolves_an_active_agent_in_one_query(env) -> None:
    dsn, store, auth = env
    admin = await _admin(dsn, auth)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", admin)
    await store.register_agent("bob")
    _, key = await service.activate("bob", TrustLevel.CONTRIBUTOR, fleet.id, admin)
    credential = await auth.verify(key)
    assert credential is not None
    assert credential.agent_name == "bob"
    assert credential.trust_level is TrustLevel.CONTRIBUTOR
    assert credential.home_fleet_id == fleet.id


# -- AUTH-7c: retired key kinds are dead ----------------------------------------


async def test_verify_rejects_pre_v2_user_and_nameless_agent_keys(env) -> None:
    dsn, _, auth = env
    await _exec(
        dsn,
        "INSERT INTO credentials (key_hash, kind, user_id, agent_id) VALUES "
        "($1, 'user', 'old', 'old-agent'), ($2, 'agent', 'old2', 'old2-agent')",
        key_hash("hm_user"),
        key_hash("hm_nameless"),
    )
    assert await auth.verify("hm_user") is None
    assert await auth.verify("hm_nameless") is None


# -- AUTH-1: activate vs revoke ---------------------------------------------------


async def test_revoke_landing_between_activate_flip_and_key_issue(env) -> None:
    """The deterministic interleaving from the review: activate flips the
    status, revoke runs to completion, then activate issues its key. The
    agent must end revoked with NO key, and activate must say so."""
    dsn, store, auth = env
    admin = await _admin(dsn, auth)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", admin)
    await store.register_agent("mallory")

    gate = asyncio.Event()
    real_issue = auth.issue_agent_key

    async def slow_issue(name: str) -> str:
        await gate.wait()
        return await real_issue(name)

    auth.issue_agent_key = slow_issue  # type: ignore[method-assign]
    activating = asyncio.create_task(
        service.activate("mallory", TrustLevel.PRIVILEGED, fleet.id, admin)
    )
    for _ in range(200):
        agent = await store.get_agent("mallory")
        if agent is not None and agent.status.value == "active":
            break
        await asyncio.sleep(0.01)
    await service.revoke("mallory", admin)
    gate.set()
    with pytest.raises(InvalidAgentStatus):
        await activating

    agent = await store.get_agent("mallory")
    assert agent is not None and agent.status.value == "revoked"
    assert await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'mallory'") == []


async def test_concurrent_activate_and_revoke_never_leave_a_live_key_on_a_revoked_agent(
    env,
) -> None:
    dsn, store, auth = env
    admin = await _admin(dsn, auth)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", admin)
    for round_no in range(25):
        name = f"agent-{round_no}"
        await store.register_agent(name)
        results = await asyncio.gather(
            service.activate(name, TrustLevel.PRIVILEGED, fleet.id, admin),
            service.revoke(name, admin),
            return_exceptions=True,
        )
        for result in results:
            assert not isinstance(result, BaseException) or isinstance(
                result, InvalidAgentStatus
            ), result
        agent = await store.get_agent(name)
        assert agent is not None
        keys = await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = $1", name)
        if agent.status.value == "revoked":
            assert keys == [], f"round {round_no}: revoked agent holds a key"
        elif agent.status.value == "active":
            assert len(keys) == 1
            activation = results[0]
            if not isinstance(activation, BaseException):
                assert await auth.verify(activation[1]) is not None


# -- AUTH-2: one live key per agent --------------------------------------------------


async def test_a_second_agent_key_row_is_refused_by_the_schema(env) -> None:
    dsn, _, _ = env
    insert = (
        "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) "
        "VALUES ($1, 'agent', 'bob', 'bob', 'bob')"
    )
    await _exec(dsn, insert, "h1")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _exec(dsn, insert, "h2")


async def test_cli_key_on_a_pending_agent_is_refused_without_activation(env) -> None:
    dsn, store, _ = env
    await store.register_agent("bob")
    with pytest.raises(AgentNeedsActivation):
        await _issue_agent(dsn, "bob", actor="t")
    assert await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'bob'") == []


async def test_cli_does_not_restore_a_revoked_agents_stale_trust(env) -> None:
    dsn, store, auth = env
    admin = await _admin(dsn, auth)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", admin)
    await store.register_agent("bob")
    await service.activate("bob", TrustLevel.PRIVILEGED, fleet.id, admin)
    await service.revoke("bob", admin)
    with pytest.raises(AgentNeedsActivation):
        await _issue_agent(dsn, "bob", actor="t")
    agent = await store.get_agent("bob")
    assert agent is not None and agent.status.value == "revoked"
    # With explicit level + fleet it is the same act as REST activate.
    key = await _issue_agent(
        dsn, "bob", actor="t", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id
    )
    agent = await store.get_agent("bob")
    assert agent is not None
    assert (agent.status.value, agent.trust_level) == ("active", TrustLevel.LURKER)
    credential = await auth.verify(key)
    assert credential is not None and credential.trust_level is TrustLevel.LURKER


async def test_cli_activation_then_rest_activate_is_a_conflict_not_a_second_key(env) -> None:
    dsn, store, auth = env
    admin = await _admin(dsn, auth)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", admin)
    await store.register_agent("bob")
    await _issue_agent(
        dsn, "bob", actor="t", trust_level=TrustLevel.CONTRIBUTOR, home_fleet_id=fleet.id
    )
    with pytest.raises(InvalidAgentStatus):
        await service.activate("bob", TrustLevel.CONTRIBUTOR, fleet.id, admin)
    audited = len(await _fetch(dsn, "SELECT 1 FROM audit_log"))
    with pytest.raises(AgentKeyExists):
        await _issue_agent(dsn, "bob", actor="t")
    assert len(await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'bob'")) == 1
    assert len(await _fetch(dsn, "SELECT 1 FROM audit_log")) == audited  # a refusal records nothing


@pytest.mark.parametrize("revoke_by", ["service", "store"])
async def test_reactivation_issues_a_fresh_key_and_replaces_a_stale_key_row(
    env, revoke_by: str
) -> None:
    """The old key stays dead. A crash between revoke's status flip and its
    key delete (``store``) leaves a dead key row; re-activation must still
    work (and replace it)."""
    dsn, store, auth = env
    admin = await _admin(dsn, auth)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", admin)
    await store.register_agent("bob")
    _, old = await service.activate("bob", TrustLevel.LURKER, fleet.id, admin)
    if revoke_by == "service":
        await service.revoke("bob", admin)
    else:
        await store.revoke_agent("bob")  # status flipped, key row left behind
    _, new = await service.activate("bob", TrustLevel.LURKER, fleet.id, admin)
    assert new != old
    assert await auth.verify(old) is None
    assert await auth.verify(new) is not None
    assert len(await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'bob'")) == 1


# -- AUTH-3: one live org key ---------------------------------------------------------


async def test_concurrent_org_rotations_leave_exactly_one_live_key(env) -> None:
    dsn, _, auth = env
    for _ in range(10):
        keys = await asyncio.gather(*(auth.rotate_org_key() for _ in range(4)))
        live = [k for k in keys if await auth.verify(k) is not None]
        assert len(live) == 1
        assert len(await _fetch(dsn, "SELECT 1 FROM credentials WHERE kind = 'org'")) == 1


async def test_concurrent_cli_and_app_rotations_leave_exactly_one_live_key(env) -> None:
    dsn, _, auth = env
    keys = await asyncio.gather(
        auth.rotate_org_key(), _rotate_org(dsn, actor="t"), auth.rotate_org_key()
    )
    live = [k for k in keys if await auth.verify(k) is not None]
    assert len(live) == 1


async def test_a_second_org_key_row_is_refused_by_the_schema(env) -> None:
    dsn, _, _ = env
    insert = "INSERT INTO credentials (key_hash, kind, user_id) VALUES ($1, 'org', 'org')"
    await _exec(dsn, insert, "o1")
    with pytest.raises(asyncpg.UniqueViolationError):
        await _exec(dsn, insert, "o2")


# -- CLI surface (main()) -------------------------------------------------------------


async def _cli(monkeypatch, *argv: str) -> None:
    import sys

    from hivemind.store import keys as keys_cli

    monkeypatch.setattr(sys, "argv", ["hivemind-keys", "--actor", "t", *argv])
    await asyncio.to_thread(keys_cli.main)


async def test_cli_flags_on_an_active_agent_are_rejected_not_ignored(
    env, monkeypatch, capsys
) -> None:
    dsn, store, auth = env
    admin = await _admin(dsn, auth)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", admin)
    await store.register_agent("bob")
    await store.activate_agent("bob", trust_level=TrustLevel.LURKER, home_fleet_id=fleet.id)
    with pytest.raises(SystemExit) as exc:
        await _cli(monkeypatch, "issue-agent", "--name", "bob", "--trust-level", "3")
    assert exc.value.code == 1
    assert "already active" in capsys.readouterr().err
    assert await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'bob'") == []


async def test_cli_bad_or_unknown_home_fleet_is_a_clean_error(env, monkeypatch, capsys) -> None:
    _, store, _ = env
    await store.register_agent("bob")
    with pytest.raises(SystemExit) as exc:
        await _cli(
            monkeypatch,
            "issue-agent",
            "--name",
            "bob",
            "--trust-level",
            "1",
            "--home-fleet",
            "not-a-uuid",
        )
    assert exc.value.code != 0
    assert "not a fleet id" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        await _cli(
            monkeypatch,
            "issue-agent",
            "--name",
            "bob",
            "--trust-level",
            "1",
            "--home-fleet",
            "00000000-0000-0000-0000-000000000000",
        )
    assert exc.value.code == 1
    assert "no fleet with id" in capsys.readouterr().err
    agent = await store.get_agent("bob")
    assert agent is not None and agent.status.value == "pending"


async def test_cli_activates_a_pending_agent_through_main(env, monkeypatch, capsys) -> None:
    dsn, store, auth = env
    admin = await _admin(dsn, auth)
    fleet = await AccessService(store, auth).create_fleet("f", admin)
    await store.register_agent("bob")
    await _cli(
        monkeypatch, "issue-agent", "--name", "bob", "--trust-level", "2", "--home-fleet", fleet.id
    )
    key = capsys.readouterr().out.strip()
    credential = await auth.verify(key)
    assert credential is not None and credential.trust_level is TrustLevel.CONTRIBUTOR


async def test_cli_list_marks_dead_rows_and_migrate_warns(env, monkeypatch, capsys, caplog) -> None:
    import logging

    dsn, store, _ = env
    await store.register_agent("pend")
    await _exec(
        dsn,
        "INSERT INTO credentials (key_hash, kind, user_id, agent_id, agent_name) VALUES "
        "('h-pend', 'agent', 'pend', 'pend', 'pend'), ('h-ghost', 'agent', 'g', 'g', 'g')",
    )
    await _exec(
        dsn, "INSERT INTO credentials (key_hash, kind, user_id) VALUES ('h-user', 'user', 'u')"
    )
    await _cli(monkeypatch, "list")
    out = capsys.readouterr().out
    assert out.count("[dead") == 3
    with caplog.at_level(logging.WARNING, logger="hivemind.store.migrate"):
        await migrate(dsn, Settings().embedding_dim)
    assert any("3 credential row(s) are dead" in r.getMessage() for r in caplog.records)


async def test_cli_refuses_a_home_fleet_that_was_merged(env) -> None:
    """ADR 0107: a merged fleet takes no new agents, on the CLI path too."""
    dsn, store, auth = env
    admin = await _admin(dsn, auth)
    service = AccessService(store, auth)
    old = await service.create_fleet("old", admin)
    new = await service.create_fleet("new", admin)
    await service.merges.merge(old.id, admin, target_fleet_id=new.id)
    await store.register_agent("bob")
    with pytest.raises(FleetMergedError) as refused:
        await _issue_agent(
            dsn, "bob", actor="t", trust_level=TrustLevel.LURKER, home_fleet_id=old.id
        )
    assert refused.value.target_id == new.id
    assert await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'bob'") == []
    await _issue_agent(dsn, "bob", actor="t", trust_level=TrustLevel.LURKER, home_fleet_id=new.id)


# -- ADR 0108: replace an active agent's key ---------------------------------------


_AUDIT = {"audit_actor_kind": ActorKind.ADMIN_KEY, "audit_actor": "admin:test"}


async def _active_bob(dsn, store, auth):
    admin = await _admin(dsn, auth)
    service = AccessService(store, auth)
    fleet = await service.create_fleet("f", admin)
    await store.register_agent("bob")
    _, old = await service.activate("bob", TrustLevel.CONTRIBUTOR, fleet.id, admin)
    return service, admin, fleet, old


async def test_replace_swaps_the_key_in_one_step_and_audits_both_fingerprints(env) -> None:
    dsn, store, auth = env
    service, admin, fleet, old = await _active_bob(dsn, store, auth)
    before = await store.get_agent("bob")
    new = await service.replace_agent_key("bob", admin)
    assert new != old
    assert await auth.verify(old) is None
    credential = await auth.verify(new)
    assert credential is not None and credential.agent_name == "bob"
    assert credential.trust_level is TrustLevel.CONTRIBUTOR
    assert credential.home_fleet_id == fleet.id
    assert await store.get_agent("bob") == before
    assert len(await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'bob'")) == 1
    [row] = await _fetch(dsn, "SELECT * FROM audit_log WHERE action = 'agent_key.replace'")
    assert (row["actor_kind"], row["target"]) == ("admin_key", "bob")
    assert json.loads(row["detail"]) == {
        "old_key": key_hash(old)[:12],
        "new_key": key_hash(new)[:12],
    }
    assert old not in row["detail"] and new not in row["detail"]


@pytest.mark.parametrize("status", ["pending", "revoked"])
async def test_replace_refuses_a_non_active_agent_and_changes_nothing(env, status) -> None:
    dsn, _, auth = env
    await _exec(dsn, "INSERT INTO agents (name, status, trust_level) VALUES ('bob', $1, 1)", status)
    with pytest.raises(InvalidAgentStatus):
        await auth.replace_agent_key("bob", **_AUDIT)
    assert await _fetch(dsn, "SELECT 1 FROM credentials") == []


async def test_replace_of_an_unknown_agent_is_refused(env) -> None:
    from hivemind.domain.access import UnknownAgent

    _, _, auth = env
    with pytest.raises(UnknownAgent):
        await auth.replace_agent_key("ghost", **_AUDIT)


async def test_concurrent_replacements_leave_exactly_one_live_key(env) -> None:
    dsn, store, auth = env
    await _active_bob(dsn, store, auth)
    results = await asyncio.gather(*(auth.replace_agent_key("bob", **_AUDIT) for _ in range(4)))
    live = [r.raw_key for r in results if await auth.verify(r.raw_key) is not None]
    assert len(live) == 1
    assert len(await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'bob'")) == 1


async def _wait_for_lock_waiters(dsn: str, count: int) -> None:
    """Until ``count`` backends of this database wait on a lock."""
    for _ in range(200):
        [row] = await _fetch(
            dsn,
            "SELECT count(*) AS c FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()",
        )
        if row["c"] >= count:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"fewer than {count} backends waiting on a lock")


@pytest.mark.parametrize("first", ["replace", "revoke"])
async def test_replace_racing_a_revoke_in_either_order_never_leaves_a_key_on_a_revoked_agent(
    env, first
) -> None:
    """Both operations queue behind a held row lock on the agent, in a forced
    order. Replace first: the key is replaced, then revoked away. Revoke first:
    the replace sees a revoked agent and is refused. Either way the agent ends
    revoked with no key row, neither key verifies, and each audit row exists
    exactly when its operation succeeded."""
    dsn, store, auth = env
    service, admin, _, old = await _active_bob(dsn, store, auth)

    async def replace() -> str:
        return await service.replace_agent_key("bob", admin)

    ops = {"replace": replace, "revoke": lambda: service.revoke("bob", admin)}
    second = "revoke" if first == "replace" else "replace"

    holder = await asyncpg.connect(dsn)
    tx = holder.transaction()
    await tx.start()
    try:
        await holder.execute("SELECT 1 FROM agents WHERE name = 'bob' FOR UPDATE")
        task_a = asyncio.create_task(ops[first]())
        await _wait_for_lock_waiters(dsn, 1)
        task_b = asyncio.create_task(ops[second]())
        await _wait_for_lock_waiters(dsn, 2)
    finally:
        await tx.commit()
        await holder.close()
    results = dict(
        zip(
            (first, second),
            await asyncio.gather(task_a, task_b, return_exceptions=True),
            strict=True,
        )
    )

    assert results["revoke"] is None
    replaced = results["replace"]
    if first == "replace":
        assert isinstance(replaced, str)
    else:
        assert isinstance(replaced, InvalidAgentStatus)
    agent = await store.get_agent("bob")
    assert agent is not None and agent.status.value == "revoked"
    assert await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'bob'") == []
    assert await auth.verify(old) is None
    if isinstance(replaced, str):
        assert await auth.verify(replaced) is None
    actions = [r["action"] for r in await _fetch(dsn, "SELECT action FROM audit_log ORDER BY id")]
    assert actions.count("agent.revoke") == 1
    assert actions.count("agent_key.replace") == (1 if isinstance(replaced, str) else 0)


async def test_cli_rotate_agent_replaces_the_key_and_audits_as_cli(
    env, monkeypatch, capsys
) -> None:
    dsn, store, auth = env
    _, _, _, old = await _active_bob(dsn, store, auth)
    await _cli(monkeypatch, "rotate-agent", "--name", "bob")
    new = capsys.readouterr().out.strip()
    assert new != old
    assert await auth.verify(old) is None
    assert await auth.verify(new) is not None
    [row] = await _fetch(dsn, "SELECT * FROM audit_log WHERE action = 'agent_key.replace'")
    assert (row["actor_kind"], row["actor"], row["target"]) == ("cli", "t", "bob")
    assert json.loads(row["detail"]) == {
        "old_key": key_hash(old)[:12],
        "new_key": key_hash(new)[:12],
    }


async def test_cli_rotate_agent_refuses_non_active_and_unknown(env, monkeypatch, capsys) -> None:
    dsn, store, _ = env
    await store.register_agent("bob")
    with pytest.raises(AgentNotActive):
        await _rotate_agent(dsn, "bob", actor="t")
    with pytest.raises(SystemExit) as exc:
        await _cli(monkeypatch, "rotate-agent", "--name", "bob")
    assert exc.value.code == 1
    assert "issue-agent" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        await _cli(monkeypatch, "rotate-agent", "--name", "ghost")
    assert exc.value.code == 1
    assert "no agent registered" in capsys.readouterr().err
    assert await _fetch(dsn, "SELECT 1 FROM credentials") == []
    assert await _fetch(dsn, "SELECT 1 FROM audit_log WHERE action = 'agent_key.replace'") == []
