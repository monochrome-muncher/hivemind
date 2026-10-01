"""`hivemind-migrate` can use its own, direct DSN (ROADMAP 3.13).

The migration lock is a session advisory lock (ADR 0020); behind a
transaction-mode PgBouncer it needs a direct connection, so
``HIVEMIND_MIGRATE_DATABASE_URL`` overrides the DSN for migrate and rollback
only. Hermetic: ``migrate`` / ``rollback`` are replaced by recorders.
"""

from __future__ import annotations

import sys

import pytest

from hivemind.config import Settings
from hivemind.store import migrate as migrate_mod

POOLED = "postgresql://hm:pw@pgbouncer:6432/hivemind"
DIRECT = "postgresql://hm:pw@postgres:5432/hivemind"


def test_migration_dsn_prefers_the_direct_dsn() -> None:
    assert Settings(database_url=POOLED).migration_dsn == POOLED
    assert Settings(database_url=POOLED, migrate_database_url="  ").migration_dsn == POOLED
    assert Settings(database_url=POOLED, migrate_database_url=DIRECT).migration_dsn == DIRECT


def test_the_direct_dsn_is_never_in_the_repr() -> None:
    assert "pw" not in repr(Settings(database_url=POOLED, migrate_database_url=DIRECT))


@pytest.mark.parametrize("argv", [[], ["--rollback", "2"]])
def test_main_migrates_and_rolls_back_over_the_direct_dsn(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    seen: list[str] = []

    async def fake_migrate(dsn: str, dim: int) -> None:
        seen.append(dsn)

    async def fake_rollback(dsn: str, dim: int, count: int) -> list[str]:
        seen.append(dsn)
        return []

    monkeypatch.setattr(migrate_mod, "migrate", fake_migrate)
    monkeypatch.setattr(migrate_mod, "rollback", fake_rollback)
    monkeypatch.setattr(
        migrate_mod,
        "load_settings",
        lambda: Settings(database_url=POOLED, migrate_database_url=DIRECT),
    )
    monkeypatch.setattr(sys, "argv", ["hivemind-migrate", *argv])
    migrate_mod.main()
    assert seen == [DIRECT]
