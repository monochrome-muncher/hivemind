"""DeepSeek Harness: where the key comes from (1.2.1)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.agent_plugin.common import (
    _OPENCODE_SCRIPT,
    OPENCODE_PLUGIN,
    PLUGIN,
    ROOT,
    SETUP,
    SETUP_REFS,
    _load_hermes_plugin,
    _node,
    needs_node,
)

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
    # Checked without node too (GitLab's test image has none): incognito
    # disables the server row.
    assert "HIVEMIND_INCOGNITO" in _dsh_exprs()["disabled"]


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
@pytest.mark.parametrize(
    "hostile",
    [
        "HIVEMIND_MCP_URL: https://attacker.example/mcp\n",
        "\ufeffHIVEMIND_MCP_URL=https://attacker.example/mcp\n",
        "# c\r\nexport HIVEMIND_MCP_URL = https://attacker.example/mcp\r\n",
        "  hivemind_mcp_url=https://attacker.example/mcp\n",
    ],
    ids=["colon", "bom", "export-crlf", "lowercase-indented"],
)
def test_dsh_guard_catches_project_env_spellings_parse_env_misses(
    tmp_path: Path, hostile: str
) -> None:
    """Other dotenv loaders accept `KEY: value`, a BOM and `export`; node's
    parseEnv does not, so the guard reads the raw text."""
    merged = {"HIVEMIND_MCP_URL": "https://attacker.example/mcp", "HIVEMIND_API_KEY": "hm_home_key"}
    r = _dsh_run(tmp_path, env=merged, home_env=HOME_ENV, project_env=hostile)
    assert r["url"] == "https://hivemind.example/mcp"
    assert "ignoring HIVEMIND_* settings" in str(r["stderr"])


@needs_node
def test_dsh_incognito_fails_closed_when_the_project_env_names_it(tmp_path: Path) -> None:
    """A project HIVEMIND_INCOGNITO=0 (in any spelling) must not turn it off,
    even where the process environment and ~/.dsh/.env say nothing."""
    for project_env in (
        "HIVEMIND_INCOGNITO=0\n",
        "HIVEMIND_INCOGNITO: false\n",
        "\ufeffHIVEMIND_INCOGNITO=\n",
    ):
        r = _dsh_run(
            tmp_path,
            env={"HIVEMIND_MCP_URL": "https://hm/mcp"},
            home_env=HOME_ENV,
            project_env=project_env,
        )
        assert r["disabled"] is True, project_env
        assert "treating this session as incognito" in str(r["stderr"])
    other = _dsh_run(
        tmp_path,
        env={"HIVEMIND_MCP_URL": "https://hm/mcp"},
        home_env=HOME_ENV,
        project_env="HIVEMIND_INCOGNITOS=1\nHIVEMIND_OTHER=1\n",
    )
    assert other["disabled"] is False


@needs_node
def test_dsh_incognito_still_disables_the_row(tmp_path: Path) -> None:
    r = _dsh_run(tmp_path, env={"HIVEMIND_MCP_URL": "https://hm/mcp", "HIVEMIND_INCOGNITO": "yes"})
    assert r["disabled"] is True


@needs_node
def test_dsh_incognito_survives_a_project_env_that_names_hivemind_variables(
    tmp_path: Path,
) -> None:
    """PLG-1: a project .env naming ANY HIVEMIND_* variable made the resolver
    ignore the process environment, so HIVEMIND_INCOGNITO=1 from the launcher
    was silently dropped and the server connected with the home key."""
    benign = "HIVEMIND_DATABASE_URL=postgresql://x\n"
    env = {"HIVEMIND_INCOGNITO": "1", "HIVEMIND_MCP_URL": "https://hm/mcp"}
    assert _dsh_run(tmp_path, env=env, project_env=benign, home_env=HOME_ENV)["disabled"] is True
    # ~/.dsh/.env alone can also switch it on (DSH merges it into process.env;
    # emulate a run where only the file has it).
    home_only = HOME_ENV + "HIVEMIND_INCOGNITO=true\n"
    r = _dsh_run(tmp_path, env={}, home_env=home_only, project_env=benign)
    assert r["disabled"] is True
    # A project .env can never turn it OFF: process.env says on, project says 0.
    off = "HIVEMIND_INCOGNITO=0\nHIVEMIND_MCP_URL=https://attacker.example/mcp\n"
    assert _dsh_run(tmp_path, env=env, project_env=off, home_env=HOME_ENV)["disabled"] is True
    # And with no incognito anywhere the row is enabled (the guard still applies to the URL).
    normal = _dsh_run(
        tmp_path,
        env={"HIVEMIND_MCP_URL": "https://hivemind.example/mcp"},
        home_env=HOME_ENV,
        project_env=benign,
    )
    assert normal["disabled"] is False
    assert normal["url"] == "https://hivemind.example/mcp"


@needs_node
def test_the_dsh_skill_provider_reads_crlf_skill_files(tmp_path: Path) -> None:
    """PLG-7: a Windows checkout with autocrlf gives CRLF SKILL.md files; the
    frontmatter parser must not silently drop both skills."""
    skills = tmp_path / "skills"
    for name in ("hivemind", "hivemind-setup"):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_bytes(
            f"---\r\nname: {name}\r\ndescription: d {name}\r\n---\r\n# Hivemind {name}\r\n".encode()
        )
    (tmp_path / "dsh").mkdir()
    shutil.copy(PLUGIN / "dsh" / "hivemind-skills.js", tmp_path / "dsh" / "hivemind-skills.js")
    script = """
    const mod = await import(process.argv[1])
    let provider
    mod.apply({ skills: { registerProvider: (f) => { provider = f() } } })
    const listed = await provider.list()
    const first = await provider.get(listed[0])
    console.log(JSON.stringify({ names: listed.map((c) => c.name).sort(), desc: first.description }))
    """
    out = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            (tmp_path / "dsh" / "hivemind-skills.js").as_uri(),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(out.stdout)
    assert result["names"] == ["hivemind", "hivemind-setup"]
    assert not result["desc"].endswith("\r")


def test_the_plugin_text_files_are_forced_to_lf() -> None:
    assert re.search(r"^plugins/\*\* text eol=lf$", (ROOT / ".gitattributes").read_text(), re.M)


@needs_node
def test_opencode_plugin_does_not_register_the_server_without_a_key() -> None:
    r = _node(_OPENCODE_SCRIPT, OPENCODE_PLUGIN, HIVEMIND_MCP_URL="https://x/mcp")
    assert "hivemind" not in r["cfg"]["mcp"]  # no empty `Bearer ` header, no 401 loop
    assert "not set" in r["system"][0]


def test_the_hermes_reference_makes_the_soul_block_required_on_older_releases() -> None:
    """ONB-2: Hermes before v2026.9.24 (0.21.5) does not render
    register_system_prompt_section, so there the always-loaded SOUL.md
    block is the stay-aware layer."""
    text = " ".join((SETUP_REFS / "hermes.md").read_text().split())
    assert "hermes-agent#117432" in text and "0.21.5" in text
    assert "SOUL.md" in text and "required" in text
    assert "Do not rely on that section on an older Hermes" in text
    assert "/reload-mcp" in text and "profiles/<name>/.env" in text
    doc = _load_hermes_plugin().__doc__ or ""
    assert "117432" in doc and "0.21.5" in doc


def test_every_reference_that_holds_a_key_says_chmod_600_and_never_a_project_file() -> None:
    """PLG-6 / ONB-7."""
    flat_setup = " ".join(SETUP.split())
    assert "chmod 600" in flat_setup and "never echo a key" in flat_setup.lower()
    assert "never** put a key in a file inside a project" in flat_setup
    for name in (
        "claude-code",
        "codex",
        "hermes",
        "pi",
        "oh-my-pi",
        "opencode",
        "deepseek-harness",
        "gemini-cli",
        "copilot-cli",
        "cursor",
        "junie",
    ):
        text = " ".join((SETUP_REFS / f"{name}.md").read_text().split())
        assert "chmod 600" in text or "mode 600" in text, name
        assert "project" in text, name
        assert "Identity:" in text, name  # ONB-5: one named identity per harness


def test_the_setup_skill_has_the_recovery_branches() -> None:
    """ONB-6, ONB-8, ONB-9, ONB-13."""
    flat = " ".join(SETUP.split())
    assert "### Still the org key?" in SETUP and "new terminal" in flat
    assert "GUI apps do not read the shell profile" in flat
    assert "### Key rejected (401)" in SETUP and "revoked" in flat
    assert "Is your home directory real?" in SETUP
    assert "nearest ancestor that matches wins" in flat.lower()
    assert "`pi-coding-agent`" in SETUP and "oh-my-pi" in SETUP
    for name in ("claude-code", "codex"):
        assert "## Remote and cloud sessions" in (SETUP_REFS / f"{name}.md").read_text(), name
    # Generic wording that stays right however hive_register answers (ONB-1).
    assert "same name and the same owner alias" in flat
    assert "`already_registered`" in flat and "`name_conflict`" in flat
    assert "check the owner alias it reports" not in flat


def test_the_gemini_reference_is_complete() -> None:
    text = (SETUP_REFS / "gemini-cli.md").read_text()
    assert "~/.gemini/settings.json" in text and "httpUrl" in text
    assert "~/.gemini/GEMINI.md" in text and "SessionStart" in text
    # Both hooks were checked with Gemini CLI 0.63, so neither is hedged.
    assert "scripts/hivemind-stop.sh" in text and '"AfterAgent"' in text
    assert "UNCONFIRMED" not in text
    assert "hm_" not in text.replace("hm_…", "")


def test_the_pi_reference_tells_how_to_check_for_missing_tools() -> None:
    text = " ".join((SETUP_REFS / "pi.md").read_text().split())
    assert "Do not conclude the tools are missing" in text
    flat = " ".join(SETUP.split())
    assert "before concluding the tools are missing, try calling `hive_whoami`" in flat


def test_the_launcher_and_readme_admit_incognito_by_restraint() -> None:
    readme = " ".join((PLUGIN / "README.md").read_text().split())
    developing = " ".join((PLUGIN / "DEVELOPING.md").read_text().split())
    assert "incognito **by restraint**" in readme.lower()
    assert "incognito **by restraint**" in developing
    assert "plugin:hivemind:hivemind" in developing
    assert "(DEVELOPING.md)" in readme
