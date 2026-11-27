"""KiloCode: an OpenCode fork, the same plugin file."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.unit.agent_plugin.common import (
    HOOK,
    INCOGNITO_TEXT,
    LAUNCHER,
    OPENCODE_PLUGIN,
    PI_EXTENSION,
    PLUGIN,
    ROOT,
    SETUP_REFS,
    SKILL_NAMES,
    SKILLS_DIR,
    STAY_AWARE_CORE,
    UPDATE_REFS,
    _fake_harness,
    _json,
    _launch,
    _node,
    needs_node,
)


def test_kilocode_loads_the_opencode_plugin_from_the_git_spec() -> None:
    """The KiloCode reference uses the bare git: spec: KiloCode loads the root
    package.json entry point, and a #subpath must be a folder, not the file."""
    ref = (SETUP_REFS / "kilocode.md").read_text()
    assert '"git:https://<git-server>/<owner>/hivemind@v<version>"' in ref
    assert "opencode/hivemind.js" not in ref
    package = json.loads((ROOT / "package.json").read_text())
    assert package["exports"]["./server"] == "./plugins/hivemind/opencode/hivemind.js"
    # The v1 plugin key is what gives the full feature set there; the docs
    # say so. The launcher's incognito overlay carries both mcp shapes:
    # KiloCode 7.8.18 kept a hand-configured server on with the v2 form
    # alone (checked live), and the v1 form switches it off.
    assert '"plugin"' in ref
    launcher = LAUNCHER.read_text()
    overlay = (
        """KILO_CONFIG_CONTENT='{"mcp":{"hivemind":{"enabled":false},"""
        """"servers":{"hivemind":{"disabled":true}}}}'"""
    )
    assert overlay in launcher


_KILOCODE_V2_SCRIPT = """
const mod = await import(process.argv[1])
// KiloCode's v2 plugin context: only options/agent/aisdk/catalog/command/
// integration/plugin/reference/skill — no mcp, session, tool or event.
// Its skill draft takes sources, not get/add.
const sources = []
const ctx = {
  options: {},
  agent: { transform: async () => {}, reload: async () => {} },
  aisdk: { sdk: async () => {}, language: async () => {} },
  catalog: { transform: async () => {}, reload: async () => {} },
  command: { transform: async () => {}, reload: async () => {} },
  integration: { transform: async () => {}, reload: async () => {} },
  plugin: { add: async () => {}, remove: async () => {} },
  reference: { transform: async () => {}, reload: async () => {} },
  skill: {
    transform: async (cb) => {
      await cb({ source: (s) => sources.push(s), list: () => sources })
    },
    reload: async () => {},
  },
}
const teardown = await mod.default.setup(ctx)
console.log(JSON.stringify({ sources, teardown: typeof teardown }))
"""


@needs_node
def test_opencode_v2_setup_degrades_on_the_kilocode_context() -> None:
    """The same file loads under KiloCode's v2 key: no mcp/session/tool/event
    domains, a source-based skill draft. It must register the skills and do
    nothing else, without raising."""
    r = _node(
        _KILOCODE_V2_SCRIPT,
        OPENCODE_PLUGIN,
        HIVEMIND_API_KEY="hm_secret_value",
        HIVEMIND_MCP_URL="https://hm.example/mcp",
    )
    # The whole skills folder goes in as one directory source (Node keeps the
    # trailing slash of the URL); nothing else is registered, and no teardown
    # is returned (no event stream to abort).
    assert r["sources"] == [{"type": "directory", "path": str(SKILLS_DIR) + "/"}]
    assert r["teardown"] == "undefined"


_SKILL_ORIGINS_SCRIPT = """
const mod = await import(process.argv[1])
const hooks = await mod.default.server({})
const cfg = JSON.parse(process.env.CFG ?? "{}")
await hooks.config(cfg)
console.log(JSON.stringify(cfg))
"""


_OTHER_ORIGIN = {"/elsewhere/skills": {"trusted": False, "source": "/elsewhere/kilo.jsonc"}}


@needs_node
def test_opencode_plugin_marks_its_skills_trusted_under_kilocode() -> None:
    cfg = _node(
        _SKILL_ORIGINS_SCRIPT,
        OPENCODE_PLUGIN,
        KILO="1",
        CFG=json.dumps({"skill_path_origins": _OTHER_ORIGIN}),
    )
    skills_dir = str(SKILLS_DIR) + "/"
    assert cfg["skills"]["paths"] == [skills_dir]
    assert cfg["skill_path_origins"] == {
        **_OTHER_ORIGIN,
        skills_dir: {"trusted": True, "source": str(OPENCODE_PLUGIN)},
    }


@needs_node
def test_opencode_plugin_leaves_skill_origins_alone_outside_kilocode() -> None:
    cfg = _node(_SKILL_ORIGINS_SCRIPT, OPENCODE_PLUGIN)
    assert cfg["skills"]["paths"] == [str(SKILLS_DIR) + "/"]
    assert "skill_path_origins" not in cfg


def test_skills_have_no_config_substitution_tokens() -> None:
    """KiloCode expands {env:NAME} and {file:path} in trusted skills, which
    could put a key into the model context, and runs !`cmd` after one prompt."""
    skills = sorted(SKILLS_DIR.rglob("SKILL.md"))
    assert [skill.parent.name for skill in skills] == SKILL_NAMES
    for skill in skills:
        text = skill.read_text()
        assert "{env:" not in text and "{file:" not in text and "!`" not in text, skill


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
    # xd:// route spellings with a suffix.
    {"toolName": "write", "input": {"path": "xd://mcp__hivemind_hive_write/"}},
    {"toolName": "write", "input": {"path": "xd://mcp__hivemind_hive_search?x=1#f"}},
    # The ADR 0058 tools, under Pi's built-in MCP names.
    {"toolName": "mcp__hivemind__hive_pinned", "input": {}},
    {"toolName": "mcp__other__hive_pin", "input": {}},
    # Oh My Pi 19.2+: tool commands run through bash.
    {"toolName": "bash", "input": {"command": "mcp__hivemind_hivemind_hive_search --query x"}},
    {"toolName": "bash", "input": {"command": "cd /tmp && mcp__hivemind_hive_write --help"}},
    {"toolName": "bash", "input": {"command": "echo hive_search; cat mcp__other_hive_pin"}},
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
        True,
        True,
        True,
        True,
        True,
        True,
        False,
    ]
    assert incognito["omp"]["systemPrompt"][-1] == INCOGNITO_TEXT


_REGISTER_SCRIPT = """
const mod = await import(process.argv[1])
const registered = []
const api = { on: () => {} }
if (process.env.HAS_API) api.registerMcpServer = (name, config) => registered.push({ name, config })
mod.default(api)
console.log(JSON.stringify({ registered }))
"""


@needs_node
def test_the_extension_registers_the_server_with_pi_built_in_mcp(tmp_path: Path) -> None:
    """Pi 0.99+: the package registers the server; not in incognito, not
    without a key, and not in a Pi (or Oh My Pi) without registerMcpServer."""
    env = {
        "HAS_API": "1",
        "HIVEMIND_MCP_URL": "https://hm.example/mcp",
        "HIVEMIND_API_KEY": "hm_x",
        "HOME": str(tmp_path),
    }
    r = _node(_REGISTER_SCRIPT, PI_EXTENSION, **env)
    [server] = r["registered"]  # type: ignore[index]
    assert server["name"] == "hivemind"
    assert server["config"]["url"] == "https://hm.example/mcp"
    assert server["config"]["headers"] == {"Authorization": "Bearer hm_x"}
    assert server["config"]["exposure"] == "direct"
    for override in ({"HIVEMIND_INCOGNITO": "1"}, {"HAS_API": ""}, {"HIVEMIND_API_KEY": ""}):
        assert _node(_REGISTER_SCRIPT, PI_EXTENSION, **{**env, **override}) == {"registered": []}


@pytest.mark.parametrize(
    ("harness", "expected"),
    [
        # Bun fills an unset or empty variable from the project's .env, so
        # Oh My Pi gets a URL that is not one.
        ("omp", ["URL=off", "KEY=unset"]),
        # Gemini fills only unset variables from the project's .env.
        ("gemini", ["URL=set-empty", "KEY=set-empty"]),
    ],
)
def test_launcher_keeps_oh_my_pi_and_gemini_from_contacting_the_server(
    tmp_path: Path, harness: str, expected: list[str]
) -> None:
    """Both expand ${HIVEMIND_*} in the plugin's or extension's server at
    startup, and both would fill a variable the launcher merely unset from
    the project's .env (checked live with Oh My Pi 18.18.1 and Gemini CLI
    0.67.0), so the launcher leaves values a project file cannot replace."""
    env = _fake_harness(tmp_path, harness)
    script = tmp_path / "bin" / harness
    script.write_text(
        "#!/bin/sh\n"
        "u=${HIVEMIND_MCP_URL-unset}; k=${HIVEMIND_API_KEY-unset}\n"
        'printf "%s\\n" "URL=${u:-set-empty}" "KEY=${k:-set-empty}"\n'
    )
    env.update(HIVEMIND_MCP_URL="https://hm.example/mcp", HIVEMIND_API_KEY="hm_x")
    assert _launch(tmp_path, env, harness) == expected


@pytest.mark.parametrize(
    ("harness", "kept"),
    [
        ("cline", False),
        ("dsh", False),
        ("opencode", False),
        ("kilo", False),
        ("some-other-agent", True),
    ],
)
def test_launcher_removes_the_key_and_url_only_where_the_server_is_kept_from_loading(
    tmp_path: Path, harness: str, kept: bool
) -> None:
    """Where the launcher keeps the server from loading, an agent with a shell
    must not be able to read the key; where it relies on restraint, the
    harness may still need both, so they stay."""
    env = _fake_harness(tmp_path, harness)
    script = tmp_path / "bin" / harness
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "URL=${HIVEMIND_MCP_URL:-unset}" "KEY=${HIVEMIND_API_KEY:-unset}" '
        '"INCOGNITO=$HIVEMIND_INCOGNITO" "$@"\n'
    )
    env.update(HIVEMIND_MCP_URL="https://hm.example/mcp", HIVEMIND_API_KEY="hm_x")
    lines = _launch(tmp_path, env, harness, "--flag")
    expected = ["URL=https://hm.example/mcp", "KEY=hm_x"] if kept else ["URL=unset", "KEY=unset"]
    assert lines == [*expected, "INCOGNITO=1", "--flag"]


def test_the_gemini_extension_serves_the_plugin() -> None:
    """`gemini extensions install <git-url>` reads gemini-extension.json,
    skills/ and hooks/hooks.json at the repository root (checked with
    Gemini CLI 0.63); skills/ is a link to the plugin's own folder."""
    ext = _json(ROOT / "gemini-extension.json")
    assert ext["name"] == "hivemind"
    assert ext["mcpServers"] == {
        "hivemind": {
            "httpUrl": "${HIVEMIND_MCP_URL}",
            "headers": {"Authorization": "Bearer ${HIVEMIND_API_KEY}"},
        }
    }
    assert (ROOT / "skills").is_symlink()
    assert (ROOT / "skills").resolve() == (PLUGIN / "skills").resolve()
    hooks = _json(ROOT / "hooks" / "hooks.json")["hooks"]
    assert set(hooks) == {"SessionStart", "AfterAgent"}
    for event, script in (
        ("SessionStart", HOOK),
        ("AfterAgent", HOOK.with_name("hivemind-stop.sh")),
    ):
        [group] = hooks[event]
        [hook] = group["hooks"]
        # Quoted, so an install path with a space is not word-split; Gemini
        # replaces ${extensionPath} as plain text, so the quotes are safe.
        prefix = 'sh "${extensionPath}/'
        assert hook["command"].startswith(prefix)
        assert hook["command"].endswith('"')
        assert (
            ROOT / hook["command"].removeprefix(prefix).removesuffix('"')
        ).resolve() == script.resolve()


def _cursor_hook(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.pop("COPILOT_CLI", None)
    env.update(**extra)
    out = subprocess.run(
        ["sh", str(HOOK), "--cursor"],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "hm_secret_value" not in out.stdout
    payload: dict[str, str] = json.loads(out.stdout)
    return payload


@pytest.mark.parametrize("key", ["hm_secret_value", None])
def test_the_hook_prints_cursors_additional_context(key: str | None) -> None:
    """The Cursor hook passes --cursor and Cursor reads a top-level
    `additional_context` from `sessionStart` (checked live with the local CLI); the nested Claude form and Copilot's camelCase are not it."""
    payload = _cursor_hook(**({"HIVEMIND_API_KEY": key} if key else {}))
    assert set(payload) == {"additional_context"}
    text = payload["additional_context"]
    assert STAY_AWARE_CORE in text
    if key:
        assert "call hive_whoami" in text
    else:
        assert "offer to run the hivemind-setup skill" in text


def test_the_hook_goes_incognito_in_cursors_shape() -> None:
    payload = _cursor_hook(HIVEMIND_INCOGNITO="1", HIVEMIND_API_KEY="hm_secret_value")
    assert payload == {"additional_context": INCOGNITO_TEXT}


def test_the_cursor_plugin_route_is_cursor_only() -> None:
    """The manifest must not let Cursor parse the Claude-format hooks file,
    and every path it names must exist."""
    kebab = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
    manifest = _json(PLUGIN / ".cursor-plugin" / "plugin.json")
    assert kebab.match(manifest["name"])  # type: ignore[arg-type]
    assert manifest["hooks"] == "./cursor/hooks.json"
    assert manifest["rules"] == "./cursor/rules/" and manifest["skills"] == "./skills/"
    assert "mcpServers" not in manifest and not (PLUGIN / "mcp.json").exists()
    for field in ("hooks", "rules", "skills"):
        assert (PLUGIN / str(manifest[field])).exists(), field
    market = _json(ROOT / ".cursor-plugin" / "marketplace.json")
    assert kebab.match(market["name"]) and market["owner"]["name"]  # type: ignore[index,call-overload,arg-type]
    [entry] = market["plugins"]  # type: ignore[misc]
    assert (ROOT / entry["source"] / ".cursor-plugin" / "plugin.json").is_file()
    hooks = _json(PLUGIN / "cursor" / "hooks.json")
    assert hooks["version"] == 1 and set(hooks["hooks"]) == {"sessionStart"}  # type: ignore[arg-type]
    [hook] = hooks["hooks"]["sessionStart"]  # type: ignore[index]
    assert hook["command"] == (
        'sh "${CURSOR_PLUGIN_ROOT}/skills/hivemind-setup/scripts/hivemind-session-start.sh" --cursor'
    )


def test_the_cursor_manifest_has_what_the_marketplace_page_shows() -> None:
    """The Cursor Marketplace wants relative paths inside the plugin and
    shows the logo, homepage, repository and license."""
    manifest = _json(PLUGIN / ".cursor-plugin" / "plugin.json")
    assert manifest["repository"] == "https://github.com/monochrome-muncher/hivemind"
    assert manifest["homepage"].startswith(manifest["repository"])
    assert manifest["license"] == "MIT"
    logo = PLUGIN / manifest["logo"]
    assert logo.read_bytes() == (ROOT / "docs" / "images" / "hivemind-icon.svg").read_bytes()
    for field in ("logo", "skills", "rules", "hooks"):
        path = manifest[field]
        assert not path.startswith("/") and ".." not in path, field
        assert (PLUGIN / path).exists(), field


def test_cursor_variables_alone_keep_the_claude_shape() -> None:
    """A Claude Code started in Cursor's terminal may inherit CURSOR_VERSION:
    only the Cursor hook's --cursor argument switches the shape."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.pop("COPILOT_CLI", None)
    env["CURSOR_VERSION"] = "2.0.0"
    out = subprocess.run(
        ["sh", str(HOOK)],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "hookSpecificOutput" in json.loads(out.stdout)
    rule = (PLUGIN / "cursor" / "rules" / "hivemind.mdc").read_text()
    assert rule.startswith("---\n") and "alwaysApply: true" in rule.split("---\n")[1]


@pytest.mark.parametrize("name", ["cursor", "cursor-agent", "agent"])
def test_launcher_unsets_the_key_and_url_for_cursor(tmp_path: Path, name: str) -> None:
    """No per-session MCP switch: the `${env:...}` references resolve empty,
    and the launcher says the rest is incognito by restraint."""
    env = _fake_harness(tmp_path, name)
    (tmp_path / "bin" / name).write_text(
        '#!/bin/sh\nprintf "%s\\n" "URL=${HIVEMIND_MCP_URL:-unset}" "KEY=${HIVEMIND_API_KEY:-unset}" '
        '"INCOGNITO=$HIVEMIND_INCOGNITO" "$@"\n'
    )
    env.update(HIVEMIND_MCP_URL="https://hm.example/mcp", HIVEMIND_API_KEY="hm_x")
    out = subprocess.run(
        ["sh", str(LAUNCHER), name, "."], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    assert out.stdout.splitlines() == ["URL=unset", "KEY=unset", "INCOGNITO=1", "."]
    assert "by restraint" in out.stderr and "hm_x" not in out.stderr


def test_the_cursor_reference_is_complete() -> None:
    text = (SETUP_REFS / "cursor.md").read_text()
    flat = " ".join(text.split())
    assert "Checked live in November 2026 with the Cursor CLI 2026.11.05" in flat
    assert "~/.cursor/mcp.json" in text and "must be named `hivemind`" in flat
    assert '"url": "${env:HIVEMIND_MCP_URL}"' in text
    assert '"Authorization": "Bearer ${env:HIVEMIND_API_KEY}"' in text
    assert "git clone https://github.com/monochrome-muncher/hivemind .hivemind-src" in text
    assert "ln -s .hivemind-src/plugins/hivemind hivemind" in text
    assert "~/.cursor/plugins/local-marketplaces.json" in text and "~/.cursor/skills/" in text
    assert "~/.cursor/rules/hivemind.mdc" in text and "alwaysApply: true" in text
    assert "additional_context" in text and "hivemind-incognito" in text
    # A marketplace install lives in a folder named after its commit, so the
    # user stop hook runs a copy at a fixed place, and the update check covers it.
    assert "Copy `scripts/hivemind-stop.sh` from this skill's folder" in text
    assert '"command": "sh \\"$HOME/.cursor/hooks/hivemind-stop.sh\\" --cursor"' in text
    update = (UPDATE_REFS / "cursor.md").read_text()
    copied = "skills/hivemind-setup/scripts/hivemind-stop.sh"
    assert f"--copy ~/.cursor/hooks/hivemind-stop.sh={copied}" in update
    assert (PLUGIN / copied).is_file()
    assert "hm_" not in text.replace("hm_…", "")


def test_launcher_starts_junie_without_its_default_mcp_files_and_with_the_incognito_text(
    tmp_path: Path,
) -> None:
    """`--mcp-default-locations=false` skips the user's and the project's
    mcp.json (checked live with Junie CLI 26.9.7), and `--ide-guidelines`
    carries the incognito text, which Junie's ignored SessionStart hook
    cannot. The key and URL are not needed either."""
    env = _fake_harness(tmp_path, "junie")
    script = tmp_path / "bin" / "junie"
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "URL=${HIVEMIND_MCP_URL:-unset}" "KEY=${HIVEMIND_API_KEY:-unset}" '
        '"INCOGNITO=$HIVEMIND_INCOGNITO" "$@"\n'
    )
    env.update(HIVEMIND_MCP_URL="https://hm.example/mcp", HIVEMIND_API_KEY="hm_x")
    lines = _launch(tmp_path, env, "junie", "--model", "m")
    assert lines[:3] == ["URL=unset", "KEY=unset", "INCOGNITO=1"]
    assert lines[3] == "--mcp-default-locations=false"
    assert lines[5:] == ["--model", "m"]
    flag = "--ide-guidelines="
    assert lines[4].startswith(flag)
    guidelines = Path(lines[4].removeprefix(flag))
    assert guidelines == PLUGIN / "junie" / "incognito.md"
    assert guidelines.read_text() == INCOGNITO_TEXT + "\n"


def test_launcher_finds_the_junie_incognito_file_through_a_symlink(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "junie")
    link = tmp_path / "linked" / "hivemind-incognito"
    link.parent.mkdir()
    link.symlink_to(LAUNCHER)
    out = subprocess.run(
        ["sh", str(link), "junie"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"--ide-guidelines={PLUGIN / 'junie' / 'incognito.md'}" in out.stdout.splitlines()


def test_the_junie_guidelines_are_the_stay_aware_line_and_the_tool_picker_sentence() -> None:
    """Junie ignores a SessionStart hook's text, so the extension's
    guidelines/ file is its always-on layer; the same file goes to the
    capability filter, which would otherwise leave the hive_* tools out."""
    text = (PLUGIN / "guidelines" / "hivemind.md").read_text()
    assert text == (
        STAY_AWARE_CORE + "\n\nRequired MCP server: hivemind. "
        "Always select all of its hive_* tools for every task.\n"
    )
    assert [p.name for p in (PLUGIN / "guidelines").iterdir()] == ["hivemind.md"]


def test_launcher_switches_the_copilot_cli_server_off_by_name(tmp_path: Path) -> None:
    """`--disable-mcp-server hivemind` keeps Copilot CLI 1.0.95 from loading
    or contacting the server (checked live); the key and URL are not needed."""
    env = _fake_harness(tmp_path, "copilot")
    script = tmp_path / "bin" / "copilot"
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "URL=${HIVEMIND_MCP_URL:-unset}" "KEY=${HIVEMIND_API_KEY:-unset}" '
        '"INCOGNITO=$HIVEMIND_INCOGNITO" "$@"\n'
    )
    env.update(HIVEMIND_MCP_URL="https://hm.example/mcp", HIVEMIND_API_KEY="hm_x")
    assert _launch(tmp_path, env, "copilot", "-p", "hi") == [
        "URL=unset",
        "KEY=unset",
        "INCOGNITO=1",
        "--disable-mcp-server",
        "hivemind",
        "-p",
        "hi",
    ]


def test_the_copilot_cli_plugin_route_uses_the_claude_manifests() -> None:
    """`copilot plugin marketplace add <repo>` reads `.claude-plugin/
    marketplace.json` at the root and the plugin's `.claude-plugin/
    plugin.json` (checked with Copilot CLI 1.0.95, which requires a
    kebab-case marketplace name, an owner name and plugin names, and runs
    the Claude-style hooks with CLAUDE_PLUGIN_ROOT set)."""
    kebab = re.compile(r"^[a-z0-9][a-z0-9.-]*$")
    market = _json(ROOT / ".claude-plugin" / "marketplace.json")
    assert kebab.match(market["name"]) and market["owner"]["name"]  # type: ignore[index,call-overload,arg-type]
    for entry in market["plugins"]:  # type: ignore[attr-defined]
        assert kebab.match(entry["name"]) and entry["description"]
    manifest = _json(PLUGIN / ".claude-plugin" / "plugin.json")
    assert kebab.match(manifest["name"]) and manifest["description"]  # type: ignore[arg-type]
    hooks = _json(PLUGIN / "hooks" / "hooks.json")["hooks"]
    assert set(hooks) == {"SessionStart", "Stop"}  # type: ignore[arg-type]
    for groups in hooks.values():  # type: ignore[attr-defined]
        for group in groups:
            for hook in group["hooks"]:
                assert "${CLAUDE_PLUGIN_ROOT}/skills/hivemind-setup/scripts/" in hook["command"]


def test_skill_descriptions_are_strict_yaml() -> None:
    """Copilot CLI parses SKILL.md frontmatter strictly: an unquoted ": " in
    the `hivemind` description made it skip the skill ("mapping values are
    not allowed in this context"), where Claude Code was lenient."""
    for folder in (PLUGIN / "skills").iterdir():
        text = (folder / "SKILL.md").read_text()
        front = yaml.safe_load(text.split("---\n")[1])
        assert front["name"] == folder.name and front["description"], folder


def test_the_copilot_cli_reference_is_complete() -> None:
    text = (SETUP_REFS / "copilot-cli.md").read_text()
    flat = " ".join(text.split())
    # The server is a user entry: Copilot rejects a whole-variable `url`.
    assert "~/.copilot/mcp-config.json" in text and "copilot mcp add" in text
    assert "Authorization: Bearer ${HIVEMIND_API_KEY}" in text
    assert "must be named `hivemind`" in flat and "--disable-mcp-server" in text
    assert "~/.copilot/copilot-instructions.md" in text and "sessionStart" in text
    assert "copilot plugin marketplace add" in text and "hivemind@hivemind" in text
    assert "no post-compaction reminder" in flat
    assert "hm_" not in text.replace("hm_…", "")
    # The plugin's own .mcp.json still reads the whole URL, for Claude Code
    # and Oh My Pi; the reference is where Copilot's limit is explained.
    mcp = _json(PLUGIN / ".mcp.json")["mcpServers"]["hivemind"]  # type: ignore[index]
    assert mcp["url"] == "${HIVEMIND_MCP_URL}"  # type: ignore[index]
    assert "cannot read the whole MCP address" in flat


def test_launcher_disables_a_hand_configured_opencode_server(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "opencode")
    script = tmp_path / "bin" / "opencode"
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$HIVEMIND_INCOGNITO" "$OPENCODE_CONFIG_CONTENT"\n'
    )
    lines = _launch(tmp_path, env, "opencode")
    assert lines[0] == "1"
    assert json.loads(lines[1]) == {"mcp": {"hivemind": {"enabled": False}}}


@pytest.mark.parametrize(
    ("harness", "var"), [("opencode", "OPENCODE_CONFIG_CONTENT"), ("kilo", "KILO_CONFIG_CONTENT")]
)
def test_launcher_keeps_an_existing_config_overlay_but_drops_the_key(
    tmp_path: Path, harness: str, var: str
) -> None:
    """The overlay is not merged in sh. When the variable is already set the
    launcher leaves it as is, unsets the key and URL so an `{env:...}` entry
    has nothing to send, and says so on stderr."""
    env = _fake_harness(tmp_path, harness)
    script = tmp_path / "bin" / harness
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "KEY=${HIVEMIND_API_KEY:-unset}" '
        f'"URL=${{HIVEMIND_MCP_URL:-unset}}" "OVERLAY=${var}"\n'
    )
    env.update(HIVEMIND_API_KEY="hm_x", HIVEMIND_MCP_URL="https://hm/mcp", **{var: '{"a":1}'})
    out = subprocess.run(
        ["sh", str(LAUNCHER), harness], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    assert out.stdout.splitlines() == ["KEY=unset", "URL=unset", 'OVERLAY={"a":1}']
    assert f"{var} is already set" in out.stderr and "literal key" in out.stderr
    assert "hm_x" not in out.stderr
    # Unset: the overlay is applied, and nothing in the session needs the key.
    del env[var]
    fresh = _launch(tmp_path, env, harness)
    assert fresh[0] == "KEY=unset" and "enabled" in fresh[2]


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
