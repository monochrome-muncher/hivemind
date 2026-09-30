"""``migrate``: a lock connection that dies mid-run is reported as a lost
lock, whatever exception type asyncpg surfaces it as (no live Postgres).

After ``pg_terminate_backend`` the next statement on the dead session can
fail as ``InterfaceError``, ``AdminShutdownError`` (a ``PostgresError``),
``ConnectionDoesNotExistError`` or even ``InternalClientError`` ("cannot
switch to state ..."), depending on timing — the integration test hit the
last one intermittently. None of them may escape as the migration's result.
"""

from __future__ import annotations

from typing import Any

import asyncpg
import pytest

from hivemind.store import migrate as m


class _DeadAfterChainConn:
    """The lock connection: grants the lock, then dies once the chain ran."""

    def __init__(self, death: BaseException) -> None:
        self._death = death
        self.chain_ran = False
        self.closed = False

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "pg_try_advisory_lock" in query:
            return True
        if self.chain_ran:
            raise self._death
        return None  # pre-chain probes: cold pool (no entries table / no extension)

    async def fetchrow(self, query: str, *args: Any) -> Any:
        return None  # cold pool: no ``entries.embedding`` column yet

    async def execute(self, query: str, *args: Any) -> str:
        if self.chain_ran:
            raise self._death
        return "SELECT 1"

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.closed = True


class _FreshConn:
    """Any other connection (e.g. the post-chain index check): healthy."""

    async def fetchval(self, query: str, *args: Any) -> Any:
        return None  # no HNSW index row -> nothing to validate

    async def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "death",
    [
        asyncpg.exceptions.InternalClientError(
            "cannot switch to state 11; another operation (2) is in progress"
        ),
        asyncpg.exceptions.AdminShutdownError("terminating connection due to administrator"),
        asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed"),
        asyncpg.exceptions.InterfaceError("connection is closed"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_a_lock_connection_lost_mid_run_is_reported_as_a_lost_lock(
    monkeypatch: pytest.MonkeyPatch, death: BaseException
) -> None:
    lock_conn = _DeadAfterChainConn(death)
    connections = iter([lock_conn])

    async def fake_connect(dsn: str) -> Any:
        return next(connections, _FreshConn())

    def fake_apply_chain(dsn: str, dim: int) -> None:
        lock_conn.chain_ran = True

    monkeypatch.setattr(m.asyncpg, "connect", fake_connect)
    monkeypatch.setattr(m, "_apply_chain", fake_apply_chain)
    monkeypatch.setattr(m, "pgvector_requirement_message", lambda version, dim: None)

    with pytest.raises(RuntimeError, match="lock connection was lost"):
        await m.migrate("postgresql://example/db", 1024)
    assert lock_conn.closed
