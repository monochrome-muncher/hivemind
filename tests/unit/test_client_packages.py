"""The `hivemind-memory` packages in clients/ stay in step with the server and the plugin
(ADR 0162): the same version, and byte-identical copies of the skill and the reminder."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENTS = ROOT / "clients"
SKILLS = ROOT / "plugins" / "hivemind" / "skills"
SKILL = SKILLS / "hivemind" / "SKILL.md"
STOP_HOOK = SKILLS / "hivemind-setup" / "scripts" / "hivemind-stop.sh"
TEXT_DIRS = (
    CLIENTS / "python" / "src" / "hivemind_memory" / "text",
    CLIENTS / "typescript" / "text",
)
SYNC_HINT = "run `make sync-client-text`"


def _server_version() -> str:
    version: str = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    return version


def _reminder() -> str:
    match = re.search(r"^text='([^']*)'$", STOP_HOOK.read_text(), re.MULTILINE)
    assert match, "the Stop hook's reminder line moved"
    return match.group(1)


def test_the_python_package_has_the_server_version() -> None:
    project = tomllib.loads((CLIENTS / "python" / "pyproject.toml").read_text())["project"]
    assert (project["name"], project["version"]) == ("hivemind-memory", _server_version())


def test_the_npm_package_and_its_lockfile_have_the_server_version() -> None:
    semver = re.sub(r"(\d)(a|b|rc)(\d+)$", r"\1-\2.\3", _server_version())
    package = json.loads((CLIENTS / "typescript" / "package.json").read_text())
    lock = json.loads((CLIENTS / "typescript" / "package-lock.json").read_text())
    assert (package["name"], package["version"]) == ("hivemind-memory", semver)
    assert (lock["version"], lock["packages"][""]["version"]) == (semver, semver)


@pytest.mark.parametrize("text_dir", TEXT_DIRS, ids=["python", "typescript"])
def test_the_skill_copy_matches_the_plugin(text_dir: Path) -> None:
    assert (text_dir / "skill.md").read_bytes() == SKILL.read_bytes(), SYNC_HINT


@pytest.mark.parametrize("text_dir", TEXT_DIRS, ids=["python", "typescript"])
def test_the_reminder_copy_matches_the_stop_hook(text_dir: Path) -> None:
    assert (text_dir / "reminder.txt").read_text() == _reminder(), SYNC_HINT
