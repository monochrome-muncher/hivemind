"""The Postgres store lane (ADR 0007: single Postgres, docker-compose).

Implements the ``Store`` and ``Authenticator`` ports on PostgreSQL +
pgvector, plus the idempotent migration runner and the key-issuance
CLI. Everything above the ports (services, retrieval, API, MCP) is
storage-agnostic (SPEC.md §4, §5).

The two factory functions (``build_store``, ``build_authenticator``)
are the seam the API layer's lazy builders import (SPEC.md §8.2): they
construct a ready adapter from ``Settings`` in a single synchronous
call (the pool opens lazily on first use, so no async work happens at
build time).
"""

from __future__ import annotations

from hivemind.config import Settings
from hivemind.ports import Authenticator, Store
from hivemind.store.auth import PgAuthenticator, key_hash
from hivemind.store.migrate import main as migrate_main
from hivemind.store.migrate import migrate
from hivemind.store.pgstore import PgStore
from hivemind.store.pool import make_pool

__all__ = [
    "PgAuthenticator",
    "PgStore",
    "build_authenticator",
    "build_store",
    "key_hash",
    "make_pool",
    "migrate",
    "migrate_main",
]


def build_store(settings: Settings) -> Store:
    """Build a ``Store`` from ``settings`` (the API's lazy builder seam).

    Returns a ``PgStore`` whose pool opens on first use; the builder
    itself is synchronous, so it is safe to call from the API's
    synchronous ``_build_store`` (SPEC.md §8.2).
    """
    return PgStore(settings.database_url)


def build_authenticator(settings: Settings) -> Authenticator:
    """Build an ``Authenticator`` from ``settings`` (ADR 0008)."""
    return PgAuthenticator(settings.database_url)
