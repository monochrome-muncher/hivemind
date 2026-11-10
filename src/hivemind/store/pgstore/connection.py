"""The pool, the connection and transaction helpers, and the contention guards every area builds on."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractContextManager, asynccontextmanager, contextmanager
from functools import partial

import asyncpg

from hivemind.domain.access import FleetBusy
from hivemind.store.pool import PoolTimeouts, make_pool

# ADR 0107: a merge or undo waits up to 5 s for in-flight writes (they share-lock
# the fleet row) and runs without the server statement timeout (client side:
# `LONG_OP_TIMEOUT`).
MERGE_SETTINGS = """
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = 0;
"""

# A merge or undo moves a whole fleet in one transaction, past the pool's 30 s command timeout.
LONG_OP_TIMEOUT = 3600.0


@contextmanager
def refuse_on_contention(
    refusal: Callable[[str], Exception], busy_message: str, deadlock_message: str
) -> Iterator[None]:
    """Turn a lock timeout or a deadlock in the block into ``refusal`` (retryable)."""
    try:
        yield
    except asyncpg.exceptions.LockNotAvailableError:
        raise refusal(busy_message) from None
    except asyncpg.exceptions.DeadlockDetectedError:
        raise refusal(deadlock_message) from None


def _write_contention(fleet_id: str | None) -> AbstractContextManager[None]:
    """``refuse_on_contention`` for a transaction that ran ``refuse_merged``: its
    ``lock_timeout`` stays set, so any later lock wait is a retryable ``FleetBusy``."""
    return refuse_on_contention(
        partial(FleetBusy, fleet_id or ""),
        "the write waited too long for a lock held by a fleet change or an agent cleanup; "
        "retry in a few seconds",
        "the write deadlocked with a concurrent change; retry in a few seconds",
    )


logger = logging.getLogger("hivemind.store.pgstore")


class PgConnections:
    """The pool and the connection helpers every area store builds on."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        timeouts: PoolTimeouts | None = None,
    ) -> None:
        self._dsn = dsn
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self._timeouts = timeouts or PoolTimeouts()
        self._acquire_timeout = self._timeouts.acquire
        self._pool: asyncpg.Pool | None = None
        # Serialises lazy creation so concurrent first calls share one pool.
        self._pool_lock = asyncio.Lock()
        # Last probe outcome, so a failure is logged once per outage (ADR 0037).
        self._database_reachable: bool | None = None

    async def _ensure_pool(self) -> asyncpg.Pool:
        """Open the pool once, even when the first calls race."""
        if self._pool is None:
            async with self._pool_lock:
                if self._pool is None:
                    self._pool = await make_pool(
                        self._dsn,
                        min_size=self._pool_min_size,
                        max_size=self._pool_max_size,
                        command_timeout=self._timeouts.command,
                        statement_timeout_ms=self._timeouts.statement_ms,
                        connect_timeout=self._timeouts.connect,
                    )
        return self._pool

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[asyncpg.Connection]:
        """A pooled connection; a saturated pool raises ``TimeoutError`` (503 upstream)."""
        pool = await self._ensure_pool()
        async with pool.acquire(timeout=self._acquire_timeout) as conn:
            yield conn

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """``_connection`` inside one transaction."""
        async with self._connection() as conn, conn.transaction():
            yield conn

    @asynccontextmanager
    async def _write_transaction(self, fleet_id: str | None) -> AsyncIterator[asyncpg.Connection]:
        """``_transaction`` for a block that runs ``refuse_merged``: contention is ``FleetBusy``."""
        with _write_contention(fleet_id):
            async with self._transaction() as conn:
                yield conn

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def health_check(self) -> bool:
        """Whether ``SELECT 1`` succeeds; never raises (the endpoint's 503 is the signal)."""
        try:
            async with self._connection() as conn:
                await conn.fetchval("SELECT 1")
        except Exception as exc:
            if self._database_reachable is not False:
                # The cause is logged once per outage, never in the probe response (ADR 0037).
                logger.warning("database health check failing: %s: %s", type(exc).__name__, exc)
            self._database_reachable = False
            return False
        if self._database_reachable is False:
            logger.info("database health check passing again")
        self._database_reachable = True
        return True
