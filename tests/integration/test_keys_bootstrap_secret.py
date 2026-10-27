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
from tests.pg import ensure_migrated

from hivemind.config import Settings
from hivemind.store import PgAuthenticator
from hivemind.store.keys import SecretLeftBehind, _bootstrap_secret, _rotate_org
from hivemind.store.kube_secret import KubeApiError

TABLES = "TRUNCATE audit_log, credentials"


class FakeSecrets:
    namespace = "hivemind"

    def __init__(
        self, *, existing: bool = False, fail_create: bool = False, fail_delete: bool = False
    ) -> None:
        self.data: dict[str, dict[str, str]] = {}
        self.labels: dict[str, str] = {}
        if existing:
            self.data["hivemind-keys"] = {}
        self._fail_create = fail_create
        self._fail_delete = fail_delete
        self.deleted: list[str] = []

    def exists(self, name: str) -> bool:
        return name in self.data

    def create(self, name: str, string_data: dict[str, str], labels: dict[str, str]) -> None:
        if self._fail_create:
            raise KubeApiError("create secret", 403, "unexpected status")
        self.data[name] = dict(string_data)
        self.labels = labels

    def delete(self, name: str) -> None:
        if self._fail_delete:
            raise KubeApiError("delete secret", 503, "unavailable")
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
    await ensure_migrated(dsn, Settings().embedding_dim)
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


class _CommitLost(ConnectionError):
    pass


@pytest.fixture
def failing_commit(dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patched after ``dsn`` has set the database up, so only the bootstrap's
    own commit fails."""

    async def commit(self: asyncpg.transaction.Transaction) -> None:
        await self.rollback()
        raise _CommitLost("connection lost during commit")

    monkeypatch.setattr(asyncpg.transaction.Transaction, "commit", commit)


@pytest.mark.usefixtures("failing_commit")
async def test_a_failed_commit_deletes_the_secret_again(dsn: str) -> None:
    secrets = FakeSecrets()
    with pytest.raises(_CommitLost):
        await _bootstrap_secret(dsn, secrets, "hivemind-keys", actor="t")
    assert secrets.deleted == ["hivemind-keys"] and secrets.data == {}
    assert await _rows(dsn, "SELECT 1 FROM credentials") == []


@pytest.mark.usefixtures("failing_commit")
async def test_a_secret_that_cannot_be_deleted_after_a_failed_commit_is_named(dsn: str) -> None:
    """Left silently, the Secret would make every later run report that it
    exists and issue nothing, while its keys never authenticate."""
    secrets = FakeSecrets(fail_delete=True)
    with pytest.raises(
        SecretLeftBehind, match="kubectl delete secret hivemind-keys -n hivemind"
    ) as exc:
        await _bootstrap_secret(dsn, secrets, "hivemind-keys", actor="t")
    assert isinstance(exc.value.__cause__, _CommitLost)
    assert "hivemind-keys" in secrets.data  # still there, hence the message
    message = f"{exc.value} {exc.value.__cause__}"
    assert all(raw not in message for raw in secrets.data["hivemind-keys"].values())
    assert await _rows(dsn, "SELECT 1 FROM credentials") == []
