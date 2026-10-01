"""Postgres-backed tests for ``hivemind-keys bootstrap-secret`` (ADR 0044).

The first-run bootstrap issues an admin key and rotates the org key straight
into a Kubernetes Secret (a fake here). The keys are committed only once the
Secret exists, so a failed Secret create leaves the database untouched, and
an existing Secret means nothing is issued at all.

Skips cleanly when Postgres is unreachable; the key and audit tables are
truncated between tests.
"""

from __future__ import annotations

import asyncpg
import pytest

from hivemind.config import Settings
from hivemind.store import PgAuthenticator
from hivemind.store.keys import _bootstrap_secret, _rotate_org
from hivemind.store.kube_secret import KubeApiError
from hivemind.store.migrate import migrate

TABLES = "TRUNCATE audit_log, credentials"


class FakeSecrets:
    def __init__(self, *, existing: bool = False, fail_create: bool = False) -> None:
        self.data: dict[str, dict[str, str]] = {}
        self.labels: dict[str, str] = {}
        if existing:
            self.data["hivemind-keys"] = {}
        self._fail_create = fail_create
        self.deleted: list[str] = []

    def exists(self, name: str) -> bool:
        return name in self.data

    def create(self, name: str, string_data: dict[str, str], labels: dict[str, str]) -> None:
        if self._fail_create:
            raise KubeApiError("create secret", 403, "unexpected status")
        self.data[name] = dict(string_data)
        self.labels = labels

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.data.pop(name, None)


def _dsn() -> str:
    return Settings().database_url


async def _exec(dsn: str, sql: str) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _rows(dsn: str, sql: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        return list(await conn.fetch(sql))
    finally:
        await conn.close()


@pytest.fixture
async def dsn():
    dsn = _dsn()
    try:
        probe = await asyncpg.connect(dsn, timeout=5)
    except Exception as exc:  # connection refused / timeout / auth
        pytest.skip(f"Postgres unreachable at {dsn}: {exc}")
    await probe.close()
    await migrate(dsn, Settings().embedding_dim)
    await _exec(dsn, TABLES)
    try:
        yield dsn
    finally:
        await _exec(dsn, TABLES)


async def test_bootstrap_stores_working_keys_in_the_secret(dsn: str) -> None:
    secrets = FakeSecrets()
    assert await _bootstrap_secret(dsn, secrets, "hivemind-keys", actor="ci-bootstrap") is True
    data = secrets.data["hivemind-keys"]
    assert set(data) == {"ADMIN_KEY", "ORG_KEY"}
    assert secrets.labels["app.kubernetes.io/name"] == "hivemind"
    auth = PgAuthenticator(dsn)
    try:
        admin = await auth.verify(data["ADMIN_KEY"])
        org = await auth.verify(data["ORG_KEY"])
    finally:
        await auth.close()
    assert admin is not None and admin.is_admin
    assert org is not None and org.is_org
    audit = await _rows(dsn, "SELECT actor, action FROM audit_log")
    assert sorted((r["actor"], r["action"]) for r in audit) == [
        ("ci-bootstrap", "admin_key.issue"),
        ("ci-bootstrap", "org_key.rotate"),
    ]


async def test_existing_secret_means_nothing_is_issued(dsn: str) -> None:
    old_org = await _rotate_org(dsn, actor="t")
    await _exec(dsn, "TRUNCATE audit_log")
    secrets = FakeSecrets(existing=True)
    assert await _bootstrap_secret(dsn, secrets, "hivemind-keys", actor="t") is False
    assert secrets.data == {"hivemind-keys": {}}
    assert await _rows(dsn, "SELECT 1 FROM audit_log") == []
    auth = PgAuthenticator(dsn)
    try:
        assert await auth.verify(old_org) is not None  # the org key was not rotated
    finally:
        await auth.close()


async def test_a_failed_secret_create_rolls_the_keys_back(dsn: str) -> None:
    old_org = await _rotate_org(dsn, actor="t")
    before = await _rows(dsn, "SELECT key_hash, kind FROM credentials ORDER BY key_hash")
    with pytest.raises(KubeApiError):
        await _bootstrap_secret(dsn, FakeSecrets(fail_create=True), "hivemind-keys", actor="t")
    after = await _rows(dsn, "SELECT key_hash, kind FROM credentials ORDER BY key_hash")
    assert after == before  # no orphan admin key, the org key unchanged
    actions = [r["action"] for r in await _rows(dsn, "SELECT action FROM audit_log")]
    assert actions == ["org_key.rotate"]  # only the setup's own rotation
    auth = PgAuthenticator(dsn)
    try:
        assert await auth.verify(old_org) is not None
    finally:
        await auth.close()
