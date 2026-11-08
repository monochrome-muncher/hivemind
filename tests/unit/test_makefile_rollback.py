"""`make rollback` passes a destructive flag only for `VAR=1` on the make
command line (ADR 0119): any other value, or the variable from the
environment, passes nothing."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

FLAGS = {
    "DISCARD_AUDIT_ROWS": "--discard-audit-rows",
    "REVIVE_REVOKED_KEYS": "--revive-revoked-keys",
    "UNLOCK_LOCKED_KEYS": "--unlock-locked-keys",
}

pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="needs make")


def _dry_run(*args: str, env: dict[str, str] | None = None) -> str:
    clean = {k: v for k, v in os.environ.items() if k not in FLAGS}
    result = subprocess.run(
        ["make", "-s", "-n", "rollback", *args],
        cwd=ROOT,
        env={**clean, **(env or {})},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


@pytest.mark.parametrize(("var", "flag"), FLAGS.items())
def test_one_on_the_command_line_passes_the_flag(var: str, flag: str) -> None:
    assert flag in _dry_run(f"{var}=1")


@pytest.mark.parametrize("value", ["0", "", "yes", "true"])
@pytest.mark.parametrize(("var", "flag"), FLAGS.items())
def test_any_other_value_passes_nothing(var: str, flag: str, value: str) -> None:
    assert flag not in _dry_run(f"{var}={value}")


@pytest.mark.parametrize(("var", "flag"), FLAGS.items())
def test_the_environment_passes_nothing(var: str, flag: str) -> None:
    assert flag not in _dry_run(env={var: "1"})


def test_no_variable_passes_no_flag() -> None:
    out = _dry_run("N=2")
    assert "--rollback 2" in out
    assert not any(flag in out for flag in FLAGS.values())
