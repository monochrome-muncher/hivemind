"""STORE-9: the integration suite TRUNCATEs / DROPs the database it is pointed at."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.db_guard import UnsafeTestDatabase, check_test_database, worker_dsn


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


def test_the_standalone_pool_name_is_refused() -> None:
    # The standalone local pool (docker-compose.standalone.yaml) is local,
    # not disposable: a test run pointed at it must refuse, not TRUNCATE.
    with pytest.raises(UnsafeTestDatabase, match="HM_TEST_ALLOW_ANY_DB"):
        check_test_database(
            "postgresql://hivemind:hivemind@localhost:5433/hivemind_live", allow_any=False
        )


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


@pytest.mark.parametrize(
    "dsn",
    [
        # libpq connects to the query's dbname, not the path's.
        "postgresql://u:p@localhost/hivemind?dbname=memory",
        "postgresql://u:p@localhost/hivemind?service=prod",
        "postgresql://u:p@localhost/hivemind?hostaddr=10.0.0.5",
        # Percent-escapes are decoded before the name is judged.
        "postgresql://u:p@localhost/hivemind_%70rod",
    ],
)
def test_a_dsn_that_hides_the_real_target_is_refused(dsn: str) -> None:
    with pytest.raises(UnsafeTestDatabase, match="HM_TEST_ALLOW_ANY_DB"):
        check_test_database(dsn, allow_any=False)


def test_the_conftest_hook_refuses_under_xdist_too() -> None:
    # Each worker vets the base before creating its own database.
    env = {k: v for k, v in os.environ.items() if k != "HM_TEST_ALLOW_ANY_DB"}
    env["HIVEMIND_DATABASE_URL"] = "postgresql://u:p@pg.prod.example.com:5432/memory"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
            "tests/integration/test_keys_cli.py",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        text=True,
        capture_output=True,
    )
    assert proc.returncode != 0
    assert "refusing to run the integration tests" in proc.stdout + proc.stderr


def test_escape_hatch_allows_any_name() -> None:
    check_test_database("postgresql://u:p@prod-db:5432/memory", allow_any=True)


def test_worker_dsn_suffixes_only_the_database_name() -> None:
    assert (
        worker_dsn("postgresql://u:p@localhost:5432/hivemind?sslmode=disable", "gw3")
        == "postgresql://u:p@localhost:5432/hivemind_gw3?sslmode=disable"
    )
    assert (
        worker_dsn("postgresql:///hivemind?host=/var/run/postgresql", "gw0")
        == "postgresql:///hivemind_gw0?host=/var/run/postgresql"
    )


def test_a_worker_database_passes_the_guard_exactly_when_its_base_does() -> None:
    for base in ("postgresql://u:p@localhost/hivemind", "postgresql://u:p@localhost/hm_test"):
        check_test_database(base, allow_any=False)
        check_test_database(worker_dsn(base, "gw0"), allow_any=False)
    # The derived name is vetted too: a suffix cannot launder a refused one.
    for base in ("postgresql://u:p@localhost/hivemind_prod", "postgresql://u:p@localhost/memory"):
        with pytest.raises(UnsafeTestDatabase):
            check_test_database(worker_dsn(base, "gw0"), allow_any=False)


def test_a_worker_database_name_that_postgres_would_truncate_is_refused() -> None:
    with pytest.raises(UnsafeTestDatabase, match="63 bytes"):
        worker_dsn(f"postgresql://u:p@localhost/hivemind_{'x' * 50}", "gw10")
    with pytest.raises(UnsafeTestDatabase, match="no database"):
        worker_dsn("postgresql://u:p@localhost", "gw0")
