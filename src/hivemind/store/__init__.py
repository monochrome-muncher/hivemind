"""The Postgres store lane (ADR 0007, SPEC §8.2): the ports on PostgreSQL + pgvector, the migration
runner, the key CLI and the synchronous factories the runners import."""

from __future__ import annotations

from hivemind.config import Settings
from hivemind.ports import Authenticator, Store
from hivemind.store.auth import PgAuthenticator, key_hash
from hivemind.store.migrate import main as migrate_main

# Renamed: ``migrate`` on the package would shadow the submodule.
from hivemind.store.migrate import migrate as apply_migrations
from hivemind.store.pgstore import PgStore
from hivemind.store.pool import PoolTimeouts, make_pool

__all__ = [
    "PgAuthenticator",
    "PgStore",
    "apply_migrations",
    "build_authenticator",
    "build_store",
    "key_hash",
    "make_pool",
    "migrate_main",
]


def _pool_timeouts(settings: Settings) -> PoolTimeouts:
    return PoolTimeouts(
        command=settings.pool_command_timeout or None,
        statement_ms=settings.pool_statement_timeout_ms,
        acquire=settings.pool_acquire_timeout or None,
        connect=settings.pool_connect_timeout,
    )


def build_store(settings: Settings) -> Store:
    """Build a ``Store`` from ``settings``; the pool opens on first use (SPEC §8.2)."""
    return PgStore(
        settings.database_url,
        pool_min_size=settings.pool_min_size,
        pool_max_size=settings.pool_max_size,
        timeouts=_pool_timeouts(settings),
    )


def build_authenticator(settings: Settings) -> Authenticator:
    """Build an ``Authenticator`` (ADR 0008) with its own pool, sized like the store's, so a
    pod's connections are the sum of both."""
    return PgAuthenticator(
        settings.database_url,
        pool_min_size=settings.pool_min_size,
        pool_max_size=settings.pool_max_size,
        timeouts=_pool_timeouts(settings),
    )
