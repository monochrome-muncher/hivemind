"""Antigravity: the plugin's own files (checked live with agy 1.5.3)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.unit.agent_plugin.common import (
    AGY,
    CONFIGURED_TEXT,
    HOOK,
    INCOGNITO_TEXT,
    PLUGIN,
    SETUP,
    STAY_AWARE_CORE,
    _json,
)


def _tree(root: Path) -> dict[str, tuple[bytes, bool]]:
    files = (p for p in sorted(root.rglob("*")) if p.is_file())
    return {str(p.relative_to(root)): (p.read_bytes(), os.access(p, os.X_OK)) for p in files}


def test_the_antigravity_skills_are_exact_copies_of_the_plugins() -> None:
    """Real files, not a link: agy 1.5.8 refuses a plugin whose symlink points
    outside its folder. `make sync-antigravity-skills` refreshes the copy."""
    assert not (AGY / "skills").is_symlink()
    assert _tree(AGY / "skills") == _tree(PLUGIN / "skills")


def test_the_antigravity_plugin_points_at_its_files() -> None:
    """A folder of its own, because Antigravity reads hooks.json at the plugin
    root, a name GitHub Copilot CLI also reads in plugins/hivemind."""
    assert not (PLUGIN / "hooks.json").exists()
    manifest = _json(AGY / "plugin.json")
    assert manifest["description"] and manifest["displayName"] == "Hivemind"
    assert (AGY / str(manifest["logo"])).is_file()
    # The schema allows no other keys (additionalProperties: false).
    allowed = {"$schema", "name", "displayName", "description", "version", "disabled", "logo"}
    allowed |= {"suggestedPrompts", "skills", "author", "homepage", "repository", "license"}
    assert set(manifest) <= allowed | {"keywords"}


def test_the_antigravity_server_takes_the_url_and_key_as_install_variables() -> None:
    """Antigravity fills ${NAME} only from a server's declared `variables`,
    which it asks for when the user turns the plugin on; no key in the file."""
    [(name, server)] = _json(AGY / "mcp_config.json")["mcpServers"].items()  # type: ignore[union-attr]
    assert name == "hivemind"  # the incognito launcher denies hivemind_hivemind
    assert server["serverUrl"] == "${HIVEMIND_MCP_URL}"
    assert server["headers"] == {"Authorization": "Bearer ${HIVEMIND_API_KEY}"}
    declared = {v["name"] for v in server["variables"]}
    assert declared == {"HIVEMIND_MCP_URL", "HIVEMIND_API_KEY"}
    assert "hm_" not in (AGY / "mcp_config.json").read_text()


def test_the_antigravity_hooks_run_the_shipped_scripts_with_the_flag() -> None:
    """Plugin hooks run from the plugin root, so the paths are relative; the
    flag selects Antigravity's output shape and skips the key check."""
    [group] = _json(AGY / "hooks.json").values()
    expected = {"SessionStart": "hivemind-session-start.sh", "Stop": "hivemind-stop.sh"}
    assert set(group) == set(expected)  # type: ignore[arg-type]
    for event, script in expected.items():
        [hook] = group[event]  # type: ignore[index]
        command = hook["command"]
        assert command == f"sh ./skills/hivemind-setup/scripts/{script} --antigravity"
        assert (AGY / command.split()[1]).is_file()


def test_the_antigravity_rule_is_the_instruction_block() -> None:
    block = re.search(r"<!-- hivemind:begin -->\n(.*?)<!-- hivemind:end -->", SETUP, re.S)
    assert block
    assert (AGY / "rules" / "AGENTS.md").read_text() == block.group(1)


@pytest.mark.parametrize("incognito", [False, True])
def test_the_session_start_hook_speaks_antigravity(incognito: bool) -> None:
    """Antigravity's hooks never see the plugin's key, and the plugin is on
    only once the key is filled in, so the text is the configured one."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.pop("COPILOT_CLI", None)
    if incognito:
        env["HIVEMIND_INCOGNITO"] = "1"
    out = subprocess.run(
        ["sh", str(HOOK), "--antigravity"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    [step] = json.loads(out.stdout)["injectSteps"]
    text = step["systemMessage"]["systemMessage"]
    if incognito:
        assert text == INCOGNITO_TEXT
    else:
        assert text.startswith(STAY_AWARE_CORE) and text.endswith(CONFIGURED_TEXT)
