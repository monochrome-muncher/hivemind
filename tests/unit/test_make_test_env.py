"""`make test` gives the suite CI's environment, not the Makefile's dev defaults.

The Makefile exports dev defaults for running the services (bind host,
strict env, embedding model, ...). Reaching the tests they made a run's
result depend on how it was started: HIVEMIND_HOST=127.0.0.1 turned on the
MCP SDK's Host check and the in-process clients got a 421. Each case runs a
copy of the Makefile in a scratch directory (so no `.env.local` is read)
with `PYTEST` swapped for `env`, and reads the environment the suite
would have seen.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")


def _suite_env(tmp_path: Path, target: str = "test", *args: str, **extra: str) -> dict[str, str]:
    shutil.copy(MAKEFILE, tmp_path / "Makefile")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("HIVEMIND_", "MAKE")) and k != "ENVIRONMENT"
    }
    env.update(extra)
    out = subprocess.run(
        # `sh -c env` ignores the pytest arguments (`tests/unit` becomes $0).
        ["make", "-s", target, "PYTEST=sh -c env", *args],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pairs = (line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {k: v for k, v in pairs if k.startswith("HIVEMIND_")}


@pytest.mark.parametrize("target", ["test", "test-unit"])
def test_only_the_dsn_and_the_dim_reach_the_suite(tmp_path: Path, target: str) -> None:
    # The two CI sets too (.github/workflows/tests.yml), matching the pool
    # `make migrate` provisions.
    assert _suite_env(tmp_path, target) == {
        "HIVEMIND_DATABASE_URL": "postgresql://hivemind:hivemind@localhost:5432/hivemind",
        "HIVEMIND_EMBEDDING_DIM": "512",
    }


def test_a_shell_export_still_reaches_the_suite(tmp_path: Path) -> None:
    env = _suite_env(tmp_path, HIVEMIND_HOST="0.0.0.0", HIVEMIND_EMBEDDING_DIM="1024")
    assert env["HIVEMIND_HOST"] == "0.0.0.0"
    assert env["HIVEMIND_EMBEDDING_DIM"] == "1024"


def test_a_command_line_value_still_reaches_the_suite(tmp_path: Path) -> None:
    env = _suite_env(tmp_path, "test", "HIVEMIND_STRICT_ENV=false")
    assert env["HIVEMIND_STRICT_ENV"] == "false"
