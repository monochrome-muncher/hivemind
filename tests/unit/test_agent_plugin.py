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
SETUP_REFS = PLUGIN / "skills" / "hivemind-setup" / "references"
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
    for text in (SKILL, SETUP, *(f.read_text() for f in SETUP_REFS.glob("*.md"))):
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
    root = _json(ROOT / "package.json")  # the DeepSeek Harness Git-URL install
    assert (root["name"], root["version"]) == (dsh["name"], semver)
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


HARNESS_REFS = {
    "Claude Code": "claude-code.md",
    "Codex": "codex.md",
    "DeepSeek Harness": "deepseek-harness.md",
    "Hermes": "hermes.md",
    "Pi": "pi.md",
    "Oh My Pi": "oh-my-pi.md",
    "OpenCode": "opencode.md",
}


def test_the_setup_skill_and_readme_cover_every_harness() -> None:
    """A new harness gets its own reference file, routed from the setup
    skill, and an install story in the README; none may be forgotten."""
    readme = (PLUGIN / "README.md").read_text()
    assert {f.name for f in SETUP_REFS.glob("*.md")} == set(HARNESS_REFS.values())
    for harness, ref in HARNESS_REFS.items():
        assert re.search(rf"^\| {re.escape(harness)}\b.*`references/{ref}` \|$", SETUP, re.M), (
            harness
        )
        text = (SETUP_REFS / ref).read_text()
        assert text.startswith(f"# Hivemind setup: {harness}\n"), ref
        flat = " ".join(text.split())
        assert f"Only for **{harness}**" in flat, ref
        assert "Which harness am I in?" in flat, ref
        for section in ("## Connect", "## Stay aware", "## Update", "## Incognito"):
            assert section in text, (ref, section)
        assert harness in readme, harness


def test_the_setup_skill_itself_is_harness_neutral() -> None:
    """Agents followed another harness's instructions (a DeepSeek model in
    OpenCode took the DeepSeek Harness steps). Harness specifics live only
    in references/, and the skill says how to tell which harness you are in."""
    for local in (
        "~/.claude",
        "~/.codex",
        "~/.dsh",
        "~/.hermes",
        "~/.pi",
        "~/.omp",
        "opencode.json",
    ):
        assert local not in SETUP, local
    flat = " ".join(SETUP.split())
    assert "Never infer the harness from your model" in flat
    assert "ask the user** which harness they are running" in flat
    # Harness names appear only in the routing section, before step 0.
    body = SETUP[SETUP.index("## 0. Where do I stand?") :]
    for harness in ("Claude Code", "Codex", "DeepSeek", "Hermes", "Oh My Pi", "OpenCode"):
        assert harness not in body, harness


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
    # Read through the project-.env-proof resolver (see the 1.2.1 tests below).
    assert "('HIVEMIND_MCP_URL')" in patch and "('HIVEMIND_API_KEY')" in patch
    assert "process.env[n]" in patch
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


# -- incognito sessions (ADR 0035) ----------------------------------------------

# The one sentence every harness's reminder layer shows in an incognito
# session, byte for byte (hook, Hermes section, Pi section).
INCOGNITO_TEXT = (
    "HIVEMIND: this is an incognito session, so Hivemind is completely off. "
    "Do not call any hive_* tool, and do not mention Hivemind or offer to set it up. "
    "You may keep local notes; start each one with [hivemind: incognito, never upload] "
    "so that no later session uploads it. If hive_* tools are loaded anyway, still do "
    "not use them, and tell the user once that the tools are loaded, so this session "
    "is incognito only by your own restraint."
)
LAUNCHER = PLUGIN / "bin" / "hivemind-incognito"


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_hook_goes_incognito(value: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.update(HIVEMIND_INCOGNITO=value, HIVEMIND_API_KEY="hm_secret_value")
    out = subprocess.run(["sh", str(HOOK)], env=env, capture_output=True, text=True, check=True)
    assert json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] == INCOGNITO_TEXT
    assert "hm_secret_value" not in out.stdout


def test_the_hook_ignores_a_false_incognito_value() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env["HIVEMIND_INCOGNITO"] = "0"
    out = subprocess.run(["sh", str(HOOK)], env=env, capture_output=True, text=True, check=True)
    assert STAY_AWARE_CORE in out.stdout


def test_the_hermes_section_goes_incognito(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEMIND_INCOGNITO", "1")
    assert _load_hermes_plugin()._hivemind_section() == INCOGNITO_TEXT


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_pi_section_goes_incognito(monkeypatch: pytest.MonkeyPatch) -> None:
    script = """
    const mod = await import(process.argv[1])
    let handler
    mod.default({ on: (name, h) => { if (name === 'before_agent_start') handler = h } })
    const event = { systemPromptOptions: { sections: {} } }
    handler(event)
    console.log(JSON.stringify(event.systemPromptOptions.sections))
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env["HIVEMIND_INCOGNITO"] = "true"
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, str(PLUGIN / "extensions" / "hivemind.ts")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout)["hivemind"] == INCOGNITO_TEXT


def test_the_dsh_row_is_off_in_incognito_sessions() -> None:
    patch = (PLUGIN / "cordis.patch.yml").read_text()
    assert "('HIVEMIND_INCOGNITO')" in patch


def _fake_harness(tmp_path: Path, name: str) -> dict[str, str]:
    """A stand-in harness on PATH that prints its args and incognito env."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / name
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "INCOGNITO=$HIVEMIND_INCOGNITO" '
        '"ENABLED=${HIVEMIND_ENABLED:-}" "MODE=${PI_MCP_CONFIG_MODE:-}" "$@"\n'
    )
    script.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.update(
        PATH=f"{bindir}:{env['PATH']}", HOME=str(tmp_path), CODEX_HOME=str(tmp_path / "codex")
    )
    return env


def _launch(tmp_path: Path, env: dict[str, str], *args: str) -> list[str]:
    out = subprocess.run(
        ["sh", str(LAUNCHER), *args],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.splitlines()


def test_launcher_denies_the_server_in_claude_code(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "claude")
    env["HIVEMIND_MCP_URL"] = "https://hm.example/mcp"
    lines = _launch(tmp_path, env, "claude", "--resume")
    assert lines[0] == "INCOGNITO=1"
    settings = json.loads(lines[lines.index("--settings") + 1])
    assert {"serverUrl": "https://hm.example/mcp"} in settings["deniedMcpServers"]
    assert lines[-1] == "--resume"


def test_launcher_disables_codex_only_when_configured(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "codex")
    assert "-c" not in _launch(tmp_path, env, "codex")  # no config: no invalid override
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").write_text('[mcp_servers.hivemind]\nurl = "x"\n')
    lines = _launch(tmp_path, env, "codex", "task")
    assert lines[lines.index("-c") + 1] == "mcp_servers.hivemind.enabled=false"


def test_launcher_turns_hermes_off_through_its_config_variable(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "hermes")
    assert "ENABLED=false" in _launch(tmp_path, env, "hermes")


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is not installed")
def test_launcher_gives_pi_an_mcp_config_without_hivemind(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "pi")
    (tmp_path / ".config" / "mcp").mkdir(parents=True)
    (tmp_path / ".config" / "mcp" / "mcp.json").write_text(
        json.dumps({"mcpServers": {"hivemind": {"url": "x"}, "github": {"url": "y"}}})
    )
    lines = _launch(tmp_path, env, "pi")
    assert "MODE=exclusive" in lines
    config = json.loads(Path(lines[lines.index("--mcp-config") + 1]).read_text())
    assert set(config["mcpServers"]) == {"github"}


def test_the_root_package_installs_the_dsh_bundle_from_a_git_url() -> None:
    """DeepSeek Harness installs a Git URL as a package from the repository
    root, so the root manifest points at the bundle's patch and ships
    everything the patch loads: the skill provider it names, and the
    skills the provider serves (it reads ../skills beside itself)."""
    root = _json(ROOT / "package.json")
    assert root["private"] is True
    assert root["type"] == "module"  # the provider is an ES module
    patch = ROOT / root["dsh"]["bundle"]["patch"]  # type: ignore[index]
    assert patch.is_file()
    shipped = [ROOT / f.rstrip("/") for f in root["files"]]  # type: ignore[union-attr]

    def is_shipped(path: Path) -> bool:
        return any(path == s or s in path.parents for s in shipped)

    needed = [
        patch,
        PLUGIN / "skills" / "hivemind" / "SKILL.md",
        PLUGIN / "skills" / "hivemind-setup" / "SKILL.md",
        *SETUP_REFS.glob("*.md"),
    ]
    needed += [patch.parent / rel for rel in re.findall(r"name: (\./\S+)", patch.read_text())]
    for path in needed:
        assert path.exists(), path
        assert is_shipped(path), f"{path.relative_to(ROOT)} is missing from package.json files"


# -- OpenCode, Oh My Pi and the shared extension (1.2.0) -----------------------

OPENCODE_PLUGIN = PLUGIN / "opencode" / "hivemind.js"
PI_EXTENSION = PLUGIN / "extensions" / "hivemind.ts"


def _node(script: str, module: Path, **env_overrides: str) -> object:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.update(env_overrides)
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script, module.as_uri()],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "hm_secret_value" not in out.stdout.replace("Bearer hm_secret_value", "")
    return json.loads(out.stdout)


_OPENCODE_SCRIPT = """
const mod = await import(process.argv[1])
const names = Object.keys(mod)
const hooks = await mod.HivemindPlugin({})
const cfg = JSON.parse(process.env.CFG ?? "{}")
await hooks.config(cfg)
const out = { system: [] }
await hooks["experimental.chat.system.transform"]({ model: {} }, out)
console.log(JSON.stringify({ names, types: names.map((n) => typeof mod[n]), cfg, system: out.system }))
"""

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@needs_node
def test_opencode_plugin_registers_the_server_and_skills() -> None:
    r = _node(
        _OPENCODE_SCRIPT,
        OPENCODE_PLUGIN,
        HIVEMIND_MCP_URL="https://hm.example/mcp",
        HIVEMIND_API_KEY="hm_secret_value",
    )
    assert r["types"] == ["function"] * len(r["names"])  # OpenCode: every export is a plugin
    server = r["cfg"]["mcp"]["hivemind"]
    assert (server["type"], server["url"], server["enabled"]) == (
        "remote",
        "https://hm.example/mcp",
        True,
    )
    assert server["headers"] == {"Authorization": "Bearer hm_secret_value"}
    assert Path(r["cfg"]["skills"]["paths"][-1]).resolve() == (PLUGIN / "skills").resolve()
    [reminder] = r["system"]
    assert reminder.startswith(STAY_AWARE_CORE) and "Hivemind is configured" in reminder


@needs_node
def test_opencode_plugin_keeps_a_hand_configured_server() -> None:
    mine = {"type": "remote", "url": "https://mine/mcp", "enabled": True}
    r = _node(
        _OPENCODE_SCRIPT,
        OPENCODE_PLUGIN,
        HIVEMIND_MCP_URL="https://x/mcp",
        CFG=json.dumps({"mcp": {"hivemind": mine}}),
    )
    assert r["cfg"]["mcp"]["hivemind"] == mine


@needs_node
def test_opencode_plugin_goes_incognito() -> None:
    fresh = _node(
        _OPENCODE_SCRIPT, OPENCODE_PLUGIN, HIVEMIND_INCOGNITO="1", HIVEMIND_MCP_URL="https://x/mcp"
    )
    assert "hivemind" not in fresh["cfg"]["mcp"]
    assert fresh["system"] == [INCOGNITO_TEXT]
    manual = _node(
        _OPENCODE_SCRIPT,
        OPENCODE_PLUGIN,
        HIVEMIND_INCOGNITO="true",
        CFG=json.dumps({"mcp": {"hivemind": {"type": "remote", "url": "u", "enabled": True}}}),
    )
    assert manual["cfg"]["mcp"]["hivemind"]["enabled"] is False


_EXTENSION_SCRIPT = """
const mod = await import(process.argv[1])
const handlers = {}
mod.default({ on: (name, h) => { handlers[name] = h } })
const omp = handlers.before_agent_start({ type: "before_agent_start", prompt: "hi", systemPrompt: ["base"] })
const calls = JSON.parse(process.env.CALLS)
console.log(JSON.stringify({ omp, blocked: calls.map((c) => handlers.tool_call(c) ?? null) }))
"""
_CALLS = [
    {"toolName": "hivemind_hive_search", "input": {}},
    {"toolName": "mcp__hivemind__hive_write", "input": {}},
    {"toolName": "write", "input": {"path": "xd://mcp__hivemind_hive_whoami", "content": "{}"}},
    {"toolName": "write", "input": {"path": "notes/hive_search.md"}},
    {"toolName": "bash", "input": {"command": "ls"}},
]


@needs_node
def test_the_extension_supports_the_oh_my_pi_prompt_shape() -> None:
    r = _node(_EXTENSION_SCRIPT, PI_EXTENSION, CALLS=json.dumps(_CALLS))
    base, section = r["omp"]["systemPrompt"]
    assert base == "base" and section.startswith(STAY_AWARE_CORE)


@needs_node
def test_the_extension_blocks_hivemind_calls_only_in_incognito() -> None:
    normal = _node(_EXTENSION_SCRIPT, PI_EXTENSION, CALLS=json.dumps(_CALLS))
    assert normal["blocked"] == [None] * len(_CALLS)
    incognito = _node(
        _EXTENSION_SCRIPT, PI_EXTENSION, CALLS=json.dumps(_CALLS), HIVEMIND_INCOGNITO="1"
    )
    assert [bool(b and b.get("block")) for b in incognito["blocked"]] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert incognito["omp"]["systemPrompt"][-1] == INCOGNITO_TEXT


def test_launcher_keeps_oh_my_pi_from_contacting_the_server(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "omp")
    script = tmp_path / "bin" / "omp"
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "URL=${HIVEMIND_MCP_URL:-unset}" "KEY=${HIVEMIND_API_KEY:-unset}"\n'
    )
    env.update(HIVEMIND_MCP_URL="https://hm.example/mcp", HIVEMIND_API_KEY="hm_x")
    assert _launch(tmp_path, env, "omp") == ["URL=unset", "KEY=unset"]


def test_launcher_disables_a_hand_configured_opencode_server(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "opencode")
    script = tmp_path / "bin" / "opencode"
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$HIVEMIND_INCOGNITO" "$OPENCODE_CONFIG_CONTENT"\n'
    )
    lines = _launch(tmp_path, env, "opencode")
    assert lines[0] == "1"
    assert json.loads(lines[1]) == {"mcp": {"hivemind": {"enabled": False}}}


def test_the_root_package_also_serves_pi_and_opencode() -> None:
    root = _json(ROOT / "package.json")
    shipped = [ROOT / f.rstrip("/") for f in root["files"]]  # type: ignore[union-attr]
    server = ROOT / root["exports"]["./server"]  # type: ignore[index]
    assert server == ROOT / root["main"] == OPENCODE_PLUGIN  # type: ignore[operator]
    assert any(s in server.parents for s in shipped)
    for rel in root["pi"]["extensions"] + root["pi"]["skills"]:  # type: ignore[index,operator]
        assert (ROOT / rel).exists(), rel
    assert "peerDependencies" not in root  # pnpm/npm would auto-install the Pi agent
    assert _json(PLUGIN / "package.json")["omp"]["extensions"] == ["./extensions/hivemind.ts"]  # type: ignore[index]


# -- DeepSeek Harness: where the key comes from (1.2.1) --------------------------

_DSH_EVAL = """
const exprs = JSON.parse(process.env.EXPRS)
const home = process.env.TEST_DSH_HOME
const path = process.getBuiltinModule('node:path')
const ctx = { process, dshHomePath: (...s) => path.join(home, ...s) }
// DSH's loader evaluates !!js exactly like this (vendor/loader/src/config/utils.ts).
const evaluate = new Function('ctx', 'expr', 'with (ctx) { return eval(expr) }')
console.log(JSON.stringify(Object.fromEntries(Object.entries(exprs).map(([k, e]) => [k, evaluate(ctx, e)]))))
"""


def _dsh_exprs() -> dict[str, str]:
    patch = (PLUGIN / "cordis.patch.yml").read_text()
    found = {
        key: json.loads(value)
        for key, value in re.findall(
            r"^\s+(disabled|url|Authorization): !!js (\".*\")$", patch, re.MULTILINE
        )
    }
    assert set(found) == {"disabled", "url", "Authorization"}
    return found


def _dsh_run(
    tmp_path: Path, *, env: dict[str, str], home_env: str = "", project_env: str = ""
) -> dict[str, object]:
    home, project = tmp_path / "dshhome", tmp_path / "project"
    home.mkdir(exist_ok=True)
    project.mkdir(exist_ok=True)
    (home / ".env").write_text(home_env)
    (project / ".env").write_text(project_env)
    base = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    out = subprocess.run(
        ["node", "-e", _DSH_EVAL],
        env={**base, **env, "EXPRS": json.dumps(_dsh_exprs()), "TEST_DSH_HOME": str(home)},
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(out.stdout)
    result["stderr"] = out.stderr
    return result


def test_the_dsh_resolver_is_the_same_in_every_field() -> None:
    found = [m for e in _dsh_exprs().values() for m in re.findall(r"\(\(n\) => \{.*?\}\)", e)]
    assert len(found) == 4  # disabled (URL + incognito), url, Authorization
    assert len(set(found)) == 1


HOME_ENV = "HIVEMIND_MCP_URL=https://hivemind.example/mcp\nHIVEMIND_API_KEY=hm_home_key\n"


@needs_node
def test_dsh_reads_the_key_from_the_launch_environment_or_dsh_home(tmp_path: Path) -> None:
    shell = _dsh_run(
        tmp_path, env={"HIVEMIND_MCP_URL": "https://hm/mcp", "HIVEMIND_API_KEY": "hm_shell"}
    )
    assert (shell["disabled"], shell["url"], shell["Authorization"]) == (
        False,
        "https://hm/mcp",
        "Bearer hm_shell",
    )
    # ~/.dsh/.env: DSH merges it into process.env at startup; emulate that.
    home = _dsh_run(
        tmp_path,
        env={"HIVEMIND_MCP_URL": "https://hivemind.example/mcp", "HIVEMIND_API_KEY": "hm_home_key"},
        home_env=HOME_ENV,
    )
    assert (home["url"], home["Authorization"]) == (
        "https://hivemind.example/mcp",
        "Bearer hm_home_key",
    )
    assert home["stderr"] == ""


@needs_node
def test_dsh_never_takes_hivemind_settings_from_a_project_env(tmp_path: Path) -> None:
    """A project .env (DSH ranks it above ~/.dsh/.env) must not choose where
    the key is sent. DSH has already merged it into process.env, which the
    resolver then ignores in favour of ~/.dsh/.env alone."""
    hostile = "HIVEMIND_MCP_URL=https://attacker.example/mcp\n"
    merged = {"HIVEMIND_MCP_URL": "https://attacker.example/mcp", "HIVEMIND_API_KEY": "hm_home_key"}
    with_home = _dsh_run(tmp_path, env=merged, home_env=HOME_ENV, project_env=hostile)
    assert with_home["url"] == "https://hivemind.example/mcp"
    assert with_home["Authorization"] == "Bearer hm_home_key"
    assert "ignoring HIVEMIND_* settings" in str(with_home["stderr"])
    assert "hm_home_key" not in str(with_home["stderr"])
    without_home = _dsh_run(tmp_path, env=merged, project_env=hostile)
    assert without_home["disabled"] is True
    assert without_home.get("url") is None


@needs_node
def test_dsh_incognito_still_disables_the_row(tmp_path: Path) -> None:
    r = _dsh_run(tmp_path, env={"HIVEMIND_MCP_URL": "https://hm/mcp", "HIVEMIND_INCOGNITO": "yes"})
    assert r["disabled"] is True
