"""The agent plugin in ``plugins/hivemind`` stays in step with the server.

The skills describe tools and ``hive_whoami`` fields by name, and the
manifests carry a version; nothing else would notice if either drifted.
"""

from __future__ import annotations

import json
import os
import re
import shutil
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

# The one sentence every harness's stay-aware layer must carry, byte for
# byte: the Claude Code/Codex SessionStart hook, the Hermes
# system-prompt section and the Pi system-prompt section all wrap this
# core, so a drift in any of the three is a drift in the contract.
STAY_AWARE_CORE = (
    "HIVEMIND: your organization's Hivemind is your long-term memory. "
    "Follow the hivemind skill (load it now if it is not in context). "
    "Recall with hive_search before non-trivial work; contribute what you "
    "learn with hive_write, as often as you have something worth "
    "reusing; prefer Hivemind over local memory files."
)


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
    dsh = _json(PLUGIN / "package.json")
    assert claude["version"] == codex["version"] == entry["version"] == dsh["version"] == semver
    assert dsh["name"] == "hivemind-dsh-plugin"
    assert _yaml_scalar(PLUGIN / "plugin.yaml", "name") == "hivemind"
    assert _yaml_scalar(PLUGIN / "plugin.yaml", "version") == semver
    [codex_entry] = _json(ROOT / ".agents" / "plugins" / "marketplace.json")["plugins"]  # type: ignore[misc]
    assert (ROOT / codex_entry["source"]["path"]).resolve() == PLUGIN
    assert (ROOT / entry["source"]).resolve() == PLUGIN


def _yaml_scalar(path: Path, key: str) -> str:
    """Read a flat top-level `key: value` from a YAML manifest.

    Deliberately no YAML dependency: plugin.yaml is flat and the drift
    this test guards is in two scalar fields.
    """
    match = re.search(rf"^{key}: (\S+)$", path.read_text(), re.MULTILINE)
    assert match, f"{key} missing from {path}"
    return match.group(1)


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
    assert STAY_AWARE_CORE in payload["additionalContext"]
    assert (
        "hive_whoami" in payload["additionalContext"]
        or "hivemind-setup" in payload["additionalContext"]
    )
    assert "hm_secret_value" not in out.stdout


def test_skill_names_match_their_folders() -> None:
    for folder in (PLUGIN / "skills").iterdir():
        text = (folder / "SKILL.md").read_text()
        assert re.search(rf"^name: {re.escape(folder.name)}$", text, re.MULTILINE), folder


def test_the_setup_skill_and_readme_cover_every_harness() -> None:
    """A new harness gets a Connect section in the setup skill and an
    install story in the README; neither may be forgotten."""
    readme = (PLUGIN / "README.md").read_text()
    for harness in ("Claude Code", "Codex", "DeepSeek Harness", "Hermes", "Pi"):
        assert f"### {harness}" in SETUP, harness
        assert harness in readme, harness


def test_the_server_instructions_name_only_real_tools() -> None:
    """The MCP ``instructions`` (shown by every harness, kept in DeepSeek
    Harness's never-compacted system prompt) must not drift from the tools."""
    from hivemind.mcp.server import _INSTRUCTIONS

    named = set(re.findall(r"\bhive_[a-z]+\b", _INSTRUCTIONS))
    assert "hive_whoami" in named
    assert named <= set(EXPECTED_TOOLS)


def test_the_dsh_bundle_patch_points_at_real_files_and_reads_keys_from_env() -> None:
    manifest = _json(PLUGIN / "package.json")
    patch_path = PLUGIN / manifest["dsh"]["bundle"]["patch"]  # type: ignore[index]
    patch = patch_path.read_text()
    for rel in re.findall(r"name: (\./\S+)", patch):
        assert (PLUGIN / rel).is_file(), rel
    assert "process.env.HIVEMIND_MCP_URL" in patch
    assert "process.env.HIVEMIND_API_KEY" in patch
    assert "hm_" not in patch  # no key ever lives in the bundle
    for included in manifest["files"]:  # type: ignore[union-attr]
        assert (PLUGIN / included).exists(), included


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_dsh_skill_provider_serves_both_skills_without_frontmatter() -> None:
    """Run the real provider module against a stand-in ``ctx.skills``."""
    script = """
    const mod = await import(process.argv[1])
    let provider
    mod.apply({ skills: { registerProvider: (factory) => { provider = factory() } } })
    const listed = await provider.list()
    const body = (await provider.get(listed.find((c) => c.name === 'hivemind'))).content
    console.log(JSON.stringify({ names: listed.map((c) => c.name).sort(), body: body.slice(0, 40) }))
    """
    module = (PLUGIN / "dsh" / "hivemind-skills.js").as_uri()
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, module],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(out.stdout)
    assert result["names"] == ["hivemind", "hivemind-setup"]
    assert result["body"].lstrip().startswith("# Hivemind")


class _HermesCtx:
    """Stand-in for Hermes' plugin ``ctx``: records what gets registered."""

    def __init__(self) -> None:
        self.skills: dict[str, Path] = {}
        self.sections: dict[str, str] = {}
        self.section_kwargs: dict[str, dict[str, object]] = {}

    def register_skill(self, name: str, skill_md: Path) -> None:
        self.skills[name] = Path(skill_md)

    def register_system_prompt_section(
        self, section_id: str, render, position: str | None = None, max_chars: int | None = None
    ) -> None:
        self.sections[section_id] = render()
        self.section_kwargs[section_id] = {"position": position, "max_chars": max_chars}


def _load_hermes_plugin() -> object:
    import importlib.util

    spec = importlib.util.spec_from_file_location("hivemind_hermes_plugin", PLUGIN / "__init__.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_hermes_plugin_serves_both_skills_and_the_stay_aware_section() -> None:
    """Run the real ``register(ctx)`` against a stand-in Hermes ctx."""
    ctx = _HermesCtx()
    _load_hermes_plugin().register(ctx)
    assert set(ctx.skills) == {"hivemind", "hivemind-setup"}
    for name, skill_md in ctx.skills.items():
        assert skill_md.is_file() and skill_md.name == "SKILL.md", name
    assert list(ctx.sections) == ["hivemind"]
    assert ctx.section_kwargs["hivemind"]["position"] == "after_memory"
    assert STAY_AWARE_CORE in ctx.sections["hivemind"]


@pytest.mark.parametrize("key", ["hm_secret_value", None])
def test_the_hermes_section_reports_the_state_and_never_leaks_the_key(
    key: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    if key is None:
        monkeypatch.delenv("HIVEMIND_API_KEY", raising=False)
    else:
        monkeypatch.setenv("HIVEMIND_API_KEY", key)
    section = _load_hermes_plugin()._hivemind_section()
    assert STAY_AWARE_CORE in section
    assert ("configured" in section) == (key is not None)
    if key is None:
        assert "hivemind-setup" in section
    assert "hm_secret_value" not in section


def test_the_pi_package_declares_real_resources() -> None:
    manifest = _json(PLUGIN / "package.json")
    pi = manifest["pi"]  # type: ignore[index]
    for rel in pi["extensions"]:  # type: ignore[union-attr]
        assert (PLUGIN / rel).is_file(), rel
    for rel in pi["skills"]:  # type: ignore[union-attr]
        assert (PLUGIN / rel).is_dir(), rel
    assert "pi-package" in manifest["keywords"]  # type: ignore[union-attr]


def _pi_extension_text() -> str:
    script = """
    const mod = await import(process.argv[1])
    let handler
    mod.default({ on: (name, h) => { if (name === 'before_agent_start') handler = h } })
    const event = { systemPromptOptions: { sections: {} } }
    handler(event)
    console.log(JSON.stringify(event.systemPromptOptions.sections))
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env["HIVEMIND_API_KEY"] = "hm_secret_value"
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN / "extensions" / "hivemind.ts")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    sections = json.loads(out.stdout)
    assert "hm_secret_value" not in out.stdout
    return sections["hivemind"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pi_extension_adds_the_stay_aware_section() -> None:
    """Run the real extension against a stand-in Pi API (Node strips the types)."""
    text = _pi_extension_text()
    assert text.startswith("HIVEMIND:")
    assert STAY_AWARE_CORE in text
    assert "configured" in text  # the test sets the key


def test_the_readme_says_what_changed_for_agents_in_this_release() -> None:
    """Every version bump adds a row to the plugin README's per-release
    table, so users can tell whether an update matters (and whether their
    own copies — the instruction block, a hand-installed hook — need a
    refresh)."""
    version = _json(PLUGIN / ".claude-plugin" / "plugin.json")["version"]
    readme = (PLUGIN / "README.md").read_text()
    table = readme.split("### What changed for agents, by release", 1)[1]
    assert re.search(rf"^\| {re.escape(str(version))} \|", table, re.MULTILINE), version
