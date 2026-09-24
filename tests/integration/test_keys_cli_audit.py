"""Postgres-backed tests for the audit log's two write paths (ADR 0027).

Path 2 — the ``hivemind-keys`` CLI: each mutating command writes exactly
one ``cli`` row **in the same transaction** as its change; ``--actor``
defaults to the OS user; ``list`` writes nothing.

Both paths — the raw-key guarantee: every key-producing action on the
CLI *and* on the app admin surface (``AccessService`` over ``PgStore`` +
``PgAuthenticator``) is performed, and no raw key appears anywhere in
any audit row's serialized content (every column, ``detail`` included).

Skips cleanly when Postgres is unreachable; the access + audit tables
are truncated between tests.
"""

from __future__ import annotations

import asyncio
import getpass
import json
import sys

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.domain import TrustLevel
from hivemind.services.access import AccessService
from hivemind.store import PgAuthenticator, PgStore
from hivemind.store import keys as keys_cli
from hivemind.store.auth import key_hash
from hivemind.store.keys import (
    AgentKeyExists,
    _issue_admin,
    _issue_agent,
    _list,
    _revoke,
    _revoke_admin,
    _rotate_org,
)
from hivemind.store.migrate import migrate

TABLES = "TRUNCATE audit_log, credentials, agents, fleets, entries, feedbacks"


def _dsn() -> str:
    return Settings().database_url


async def _prepare(dsn: str) -> None:
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, Settings().embedding_dim)
    await _exec(dsn, TABLES)


async def _exec(dsn: str, sql: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _fetch(dsn: str, sql: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        return list(await conn.fetch(sql))
    finally:
        await conn.close()


async def _audit_rows(dsn: str) -> list[asyncpg.Record]:
    return await _fetch(
        dsn,
        "SELECT actor_kind, actor, action, target, detail FROM audit_log ORDER BY occurred_at",
    )


@pytest.fixture
async def dsn():
    dsn = _dsn()
    await _prepare(dsn)
    try:
        yield dsn
    finally:
        await _exec(dsn, TABLES)


# -- one row per CLI command, in the mutation's transaction ------------------


async def test_issue_admin_records_the_fingerprint(dsn: str) -> None:
    raw = await _issue_admin(dsn, actor="chris")
    [row] = await _audit_rows(dsn)
    assert (row["actor_kind"], row["actor"], row["action"]) == ("cli", "chris", "admin_key.issue")
    assert row["target"] == key_hash(raw)[:12]


async def test_issue_agent_records_the_agent_name(dsn: str) -> None:
    await _issue_agent(dsn, "alice", actor="chris")
    [row] = await _audit_rows(dsn)
    assert (row["action"], row["target"]) == ("agent_key.issue", "alice")


async def test_rotate_org_records_no_target(dsn: str) -> None:
    await _rotate_org(dsn, actor="chris")
    [row] = await _audit_rows(dsn)
    assert (row["action"], row["target"]) == ("org_key.rotate", None)


async def test_revoke_records_the_agent_name(dsn: str) -> None:
    await _issue_agent(dsn, "alice", actor="chris")
    await _revoke(dsn, "alice", actor="ops")
    rows = await _audit_rows(dsn)
    assert [(r["actor"], r["action"], r["target"]) for r in rows] == [
        ("chris", "agent_key.issue", "alice"),
        ("ops", "agent.revoke", "alice"),
    ]


@pytest.mark.parametrize("by", ["raw_key", "stored_hash"])
async def test_revoke_admin_normalises_to_the_fingerprint(dsn: str, by: str) -> None:
    raw = await _issue_admin(dsn, actor="chris")
    ident = {"raw_key": raw} if by == "raw_key" else {"stored_hash": key_hash(raw)}
    assert await _revoke_admin(dsn, actor="ops", **ident) is True
    revoke = (await _audit_rows(dsn))[-1]
    assert revoke["action"] == "admin_key.revoke"
    assert revoke["target"] == key_hash(raw)[:12]


async def test_a_revoke_admin_miss_records_nothing(dsn: str) -> None:
    assert await _revoke_admin(dsn, raw_key="hm_never_issued", actor="ops") is False
    assert await _audit_rows(dsn) == []


async def test_list_records_nothing(dsn: str) -> None:
    await _list(dsn)
    assert await _audit_rows(dsn) == []


async def test_the_audit_row_shares_the_mutations_transaction(
    dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the audit insert fails, the key issuance rolls back with it."""
    # `$1 || 'x'` turns 'cli' into 'clix', which the named CHECK rejects.
    monkeypatch.setattr(
        keys_cli,
        "_INSERT_AUDIT",
        "INSERT INTO audit_log (actor_kind, actor, action, target, detail) "
        "VALUES ($1 || 'x', $2, $3, $4, $5::jsonb)",
    )
    for command in (
        lambda: _issue_admin(dsn, actor="chris"),
        lambda: _issue_agent(dsn, "alice", actor="chris"),
        lambda: _rotate_org(dsn, actor="chris"),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await command()
    assert await _fetch(dsn, "SELECT 1 FROM credentials") == []
    assert await _audit_rows(dsn) == []


def test_actor_defaults_to_the_os_user(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """Through ``main()`` — the real argparse surface, no ``--actor``."""
    dsn = _dsn()
    asyncio.run(_prepare(dsn))
    try:
        monkeypatch.setattr(sys, "argv", ["hivemind-keys", "rotate-org"])
        keys_cli.main()
        assert capsys.readouterr().out.startswith("hm_")
        [row] = asyncio.run(_audit_rows(dsn))
        assert row["actor"] == getpass.getuser()
        assert row["actor_kind"] == "cli"

        monkeypatch.setattr(sys, "argv", ["hivemind-keys", "--actor", "break-glass", "rotate-org"])
        keys_cli.main()
        assert asyncio.run(_audit_rows(dsn))[-1]["actor"] == "break-glass"
    finally:
        asyncio.run(_exec(dsn, TABLES))


# -- no raw key in any audit row, across BOTH paths ---------------------------


async def test_no_raw_key_appears_in_any_audit_row_on_either_path(dsn: str) -> None:
    raw_keys: list[str] = []

    # Path 2 — the CLI: every key-producing command, plus both revokes.
    admin_key = await _issue_admin(dsn, actor="chris")
    spare_admin = await _issue_admin(dsn, actor="chris")
    raw_keys += [admin_key, spare_admin]
    raw_keys.append(await _issue_agent(dsn, "bob", actor="chris"))
    raw_keys.append(await _rotate_org(dsn, actor="chris"))
    assert await _revoke_admin(dsn, raw_key=spare_admin, actor="chris") is True
    await _revoke(dsn, "bob", actor="chris")

    # Path 1 — the app admin surface over the real adapters, acting with
    # the CLI-issued admin key as verified by PgAuthenticator.
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    try:
        admin = await auth.verify(admin_key)
        assert admin is not None and admin.key_id == key_hash(admin_key)[:12]
        service = AccessService(store, auth)
        await store.register_agent("alice")
        fleet = await service.create_fleet("data-eng", admin)
        _agent, agent_key = await service.activate("alice", TrustLevel.CONTRIBUTOR, fleet.id, admin)
        raw_keys.append(agent_key)
        raw_keys.append(await service.rotate_org_key(admin))
        await service.set_trust_level("alice", TrustLevel.PRIVILEGED, admin)
        await service.revoke("alice", admin)
    finally:
        await store.close()
        await auth.close()

    rows = await _fetch(dsn, "SELECT row_to_json(a)::text AS doc FROM audit_log a")
    assert len(rows) == 11  # 6 CLI rows + 5 app rows
    docs = [r["doc"] for r in rows]
    for raw in raw_keys:
        assert raw.startswith("hm_")
        for doc in docs:
            assert raw not in doc
            # Nor the secret part without its prefix.
            assert raw.removeprefix("hm_") not in doc
    kinds = await _fetch(dsn, "SELECT actor_kind, count(*) AS n FROM audit_log GROUP BY 1")
    assert {r["actor_kind"]: r["n"] for r in kinds} == {"cli": 6, "admin_key": 5}


# -- ADR 0028: the CLI keeps status and key in step --------------------------


async def test_cli_revoke_sets_revoked_and_records_the_prior_status(dsn: str) -> None:
    await _exec(dsn, "INSERT INTO agents (name, status, trust_level) VALUES ('alice', 'active', 2)")
    await _issue_agent(dsn, "alice", actor="chris")
    await _revoke(dsn, "alice", actor="ops")
    [status] = await _fetch(dsn, "SELECT status FROM agents WHERE name = 'alice'")
    assert status["status"] == "revoked"
    assert await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'alice'") == []
    revoke_row = (await _audit_rows(dsn))[-1]
    assert revoke_row["action"] == "agent.revoke"
    assert json.loads(revoke_row["detail"]) == {"from": "active"}


async def test_cli_issue_agent_refuses_a_second_key(dsn: str) -> None:
    await _issue_agent(dsn, "alice", actor="chris")
    with pytest.raises(AgentKeyExists):
        await _issue_agent(dsn, "alice", actor="chris")
    assert len(await _fetch(dsn, "SELECT 1 FROM credentials WHERE agent_name = 'alice'")) == 1
    assert len(await _audit_rows(dsn)) == 1  # the refusal recorded nothing


async def test_cli_issue_agent_reactivates_a_revoked_agent(dsn: str) -> None:
    await _exec(
        dsn, "INSERT INTO agents (name, status, trust_level) VALUES ('alice', 'revoked', 1)"
    )
    await _issue_agent(dsn, "alice", actor="chris")
    [status] = await _fetch(dsn, "SELECT status FROM agents WHERE name = 'alice'")
    assert status["status"] == "active"


async def test_app_reactivation_issues_a_fresh_key_and_the_old_one_stays_dead(
    dsn: str,
) -> None:
    store, auth = PgStore(dsn), PgAuthenticator(dsn)
    try:
        admin = await auth.verify(await _issue_admin(dsn, actor="chris"))
        assert admin is not None
        service = AccessService(store, auth)
        fleet = await service.create_fleet("data-eng", admin)
        await store.register_agent("alice")
        _, old = await service.activate("alice", TrustLevel.LURKER, fleet.id, admin)
        await service.revoke("alice", admin)
        _, new = await service.activate("alice", TrustLevel.LURKER, fleet.id, admin)
        assert new != old
        assert await auth.verify(old) is None
        assert await auth.verify(new) is not None
    finally:
        await store.close()
        await auth.close()
