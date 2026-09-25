"""The agent plugin in ``plugins/hivemind`` stays in step with the server.

The skills describe tools and ``hive_whoami`` fields by name, and the
manifests carry a version; nothing else would notice if either drifted.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

from hivemind.domain.access import Standing, TrustLevel
from tests.unit.test_mcp_server import EXPECTED_TOOLS

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "hivemind"
SKILL = (PLUGIN / "skills" / "hivemind" / "SKILL.md").read_text()
SETUP = (PLUGIN / "skills" / "hivemind-setup" / "SKILL.md").read_text()
HOOK = PLUGIN / "skills" / "hivemind-setup" / "scripts" / "hivemind-session-start.sh"


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


def test_every_mcp_tool_is_in_the_skill_and_no_other() -> None:
    mentioned = set(re.findall(r"\bhive_[a-z]+\b", SKILL))
    assert mentioned == set(EXPECTED_TOOLS)


def test_the_whoami_fields_the_skills_rely_on_exist() -> None:
    fields = Standing(
        key_kind="agent",
        name="a",
        status=None,
        trust_level=TrustLevel.LURKER,
        home_fleet_id=None,
        home_fleet_name=None,
        can_read=(),
        can_write_scopes=(),
    ).as_dict()
    for text in (SKILL, SETUP):
        # Field names only: `home_fleet` alone is a can_read *value*.
        for field in re.findall(r"`(key_kind|trust_level\w*|can_\w+|home_fleet_\w+)\b", text):
            assert field in fields, field


def test_manifests_agree_on_name_and_version() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    # PEP 440 "1.0.0rc1" is SemVer "1.0.0-rc.1" in the plugin manifests.
    semver = re.sub(r"(\d)(a|b|rc)(\d+)$", r"\1-\2.\3", project)
    claude = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    codex = _json(PLUGIN / ".codex-plugin" / "plugin.json")
    market = _json(ROOT / ".claude-plugin" / "marketplace.json")
    [entry] = market["plugins"]  # type: ignore[misc]
    assert claude["name"] == codex["name"] == entry["name"] == "hivemind"
    assert claude["version"] == codex["version"] == entry["version"] == semver
    [codex_entry] = _json(ROOT / ".agents" / "plugins" / "marketplace.json")["plugins"]  # type: ignore[misc]
    assert (ROOT / codex_entry["source"]["path"]).resolve() == PLUGIN
    assert (ROOT / entry["source"]).resolve() == PLUGIN


def test_the_hook_runs_the_shipped_script_on_every_context_rebuild() -> None:
    [session_start] = _json(PLUGIN / "hooks" / "hooks.json")["hooks"]["SessionStart"]  # type: ignore[index]
    assert set(session_start["matcher"].split("|")) == {"startup", "resume", "clear", "compact"}
    [hook] = session_start["hooks"]
    assert "skills/hivemind-setup/scripts/hivemind-session-start.sh" in hook["command"]


@pytest.mark.parametrize("key", ["hm_secret_value", None])
def test_the_hook_prints_valid_json_and_never_the_key(key: str | None) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    if key is not None:
        env["HIVEMIND_API_KEY"] = key
    out = subprocess.run(["sh", str(HOOK)], env=env, capture_output=True, text=True, check=True)
    payload = json.loads(out.stdout)["hookSpecificOutput"]
    assert payload["hookEventName"] == "SessionStart"
    assert payload["additionalContext"].startswith("HIVEMIND:")
    assert (
        "hive_whoami" in payload["additionalContext"]
        or "hivemind-setup" in payload["additionalContext"]
    )
    assert "hm_secret_value" not in out.stdout


def test_skill_names_match_their_folders() -> None:
    for folder in (PLUGIN / "skills").iterdir():
        text = (folder / "SKILL.md").read_text()
        assert re.search(rf"^name: {re.escape(folder.name)}$", text, re.MULTILINE), folder
