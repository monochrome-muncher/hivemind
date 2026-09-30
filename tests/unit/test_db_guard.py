"""STORE-9: the integration suite TRUNCATEs / DROPs the database it is pointed at."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.db_guard import UnsafeTestDatabase, check_test_database


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://hivemind:hivemind@localhost:5432/hivemind",  # the dev default
        "postgresql://hivemind:hivemind@localhost:5432/hivemind_a7",
        "postgresql://u:p@localhost:5432/hivemind_test",
        "postgresql://u:p@localhost:5432/ci?sslmode=disable",
        "postgresql://u:p@localhost:5432/scratch_dev",
    ],
)
def test_dev_and_test_database_names_are_accepted(dsn: str) -> None:
    check_test_database(dsn, allow_any=False)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@prod-db:5432/memory",
        "postgresql://u:p@prod-db:5432/hivemind-production",
        "postgresql://u:p@prod-db:5432/postgres",
    ],
)
def test_other_database_names_are_refused(dsn: str) -> None:
    with pytest.raises(UnsafeTestDatabase, match="HM_TEST_ALLOW_ANY_DB"):
        check_test_database(dsn, allow_any=False)


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:p@pg.prod.example.com:5432/hivemind",  # DEPLOY.md's production name
        "postgresql://hivemind:x@pg.hivemind.svc:5432/hivemind",
        "postgresql://u:p@10.0.0.5/hivemind_test",
    ],
)
def test_remote_hosts_are_refused_even_with_a_dev_looking_name(dsn: str) -> None:
    with pytest.raises(UnsafeTestDatabase, match="HM_TEST_ALLOW_ANY_DB"):
        check_test_database(dsn, allow_any=False)


def test_unix_socket_host_is_accepted() -> None:
    check_test_database("postgresql:///hivemind?host=/var/run/postgresql", allow_any=False)


def test_the_conftest_hook_is_wired_into_pytest() -> None:
    # Revert-sensitive: collecting the integration tests against a production-
    # looking DSN must abort before any test can run.
    env = {k: v for k, v in os.environ.items() if k != "HM_TEST_ALLOW_ANY_DB"}
    env["HIVEMIND_DATABASE_URL"] = "postgresql://u:p@pg.prod.example.com:5432/memory"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "tests/integration/test_keys_cli.py"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
    )
    assert proc.returncode != 0
    assert "refusing to run the integration tests" in proc.stdout + proc.stderr


def test_escape_hatch_allows_any_name() -> None:
    check_test_database("postgresql://u:p@prod-db:5432/memory", allow_any=True)
