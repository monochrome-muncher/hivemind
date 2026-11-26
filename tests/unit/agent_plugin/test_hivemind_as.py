"""hivemind-as: one profile per fleet (ADR 0150)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.unit.agent_plugin.common import (
    LAUNCHER,
    PLUGIN,
    ROOT,
    SETUP_REFS,
    UPDATE_REFS,
    _fake_harness,
    _json,
    _launch,
)

SWITCHER = PLUGIN / "bin" / "hivemind-as"


SWITCH_SKILL = (PLUGIN / "skills" / "hivemind-switch" / "SKILL.md").read_text()


def _profiles(tmp_path: Path, *names: str) -> tuple[dict[str, str], Path]:
    """Stand-in harnesses that print the key, URL, profile and arguments,
    and an empty ~/.config/hivemind."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name in names:
        script = bindir / name
        script.write_text(
            '#!/bin/sh\nprintf "%s\\n" "KEY=${HIVEMIND_API_KEY:-unset}" '
            '"URL=${HIVEMIND_MCP_URL:-unset}" "PROFILE=${HIVEMIND_PROFILE:-}" "$@"\n'
        )
        script.chmod(0o755)
    config = tmp_path / ".config" / "hivemind"
    config.mkdir(parents=True)
    config.chmod(0o700)
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.update(PATH=f"{bindir}:{env['PATH']}", HOME=str(tmp_path), HIVEMIND_API_KEY="hm_inherited")
    return env, config


def _key_file(path: Path, key: str, url: str = "https://hm.example/mcp") -> None:
    path.write_text(f'export HIVEMIND_MCP_URL="{url}"\nexport HIVEMIND_API_KEY="{key}"\n')
    path.chmod(0o600)


def _switch(
    env: dict[str, str], cwd: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SWITCHER), *args], env=env, cwd=cwd, capture_output=True, text=True, check=check
    )


def test_hivemind_as_starts_a_harness_with_the_profiles_key(tmp_path: Path) -> None:
    env, config = _profiles(tmp_path, "codex")
    _key_file(config / "codex.client.env", "hm_client")
    out = _switch(env, tmp_path, "client", "codex", "fix it")
    assert out.stdout.splitlines() == [
        "KEY=hm_client",
        "URL=https://hm.example/mcp",
        "PROFILE=client",
        "fix it",
    ]
    assert "profile client" in out.stderr and "hm_client" not in out.stderr


@pytest.mark.parametrize("name", ["cursor-agent", "agent"])
def test_hivemind_as_reads_the_cursor_profile_for_cursors_cli(tmp_path: Path, name: str) -> None:
    env, config = _profiles(tmp_path, name)
    _key_file(config / "cursor.client.env", "hm_client")
    out = _switch(env, tmp_path, "client", name)
    assert out.stdout.splitlines()[0] == "KEY=hm_client"


def test_hivemind_as_never_falls_back_to_an_inherited_key(tmp_path: Path) -> None:
    """A profile file without a key must fail, not run as the shell's agent."""
    env, config = _profiles(tmp_path, "codex")
    (config / "codex.client.env").write_text('export HIVEMIND_MCP_URL="https://hm/mcp"\n')
    (config / "codex.client.env").chmod(0o600)
    out = _switch(env, tmp_path, "client", "codex", check=False)
    assert out.returncode == 2 and out.stdout == ""
    assert "must set both" in out.stderr


def test_hivemind_as_passes_claude_codes_profile_as_a_settings_file(tmp_path: Path) -> None:
    """settings.json's env beats the shell, so Claude Code gets the profile
    through --settings, a file path: the key is never on the command line."""
    env, config = _profiles(tmp_path, "claude")
    profile = config / "claude.client.json"
    profile.write_text(json.dumps({"env": {"HIVEMIND_API_KEY": "hm_client"}}))
    profile.chmod(0o600)
    lines = _switch(env, tmp_path, "client", "claude", "--resume").stdout.splitlines()
    assert lines == [
        "KEY=unset",
        "URL=unset",
        "PROFILE=client",
        "--settings",
        str(profile),
        "--resume",
    ]


def test_hivemind_as_uses_hermes_own_profiles(tmp_path: Path) -> None:
    env, _ = _profiles(tmp_path, "hermes")
    lines = _switch(env, tmp_path, "client", "hermes", "chat").stdout.splitlines()
    assert lines == ["KEY=unset", "URL=unset", "PROFILE=client", "-p", "client", "chat"]
    # "default" overrides a profile `hermes profile use` saved.
    assert _switch(env, tmp_path, "default", "hermes").stdout.splitlines()[-2:] == ["-p", "default"]


def test_hivemind_as_default_keeps_the_usual_setup(tmp_path: Path) -> None:
    env, config = _profiles(tmp_path, "codex", "claude")
    assert _switch(env, tmp_path, "default", "codex").stdout.splitlines()[0] == "KEY=hm_inherited"
    assert "--settings" not in _switch(env, tmp_path, "default", "claude").stdout
    _key_file(config / "codex.env", "hm_codex")
    assert _switch(env, tmp_path, "default", "codex").stdout.splitlines()[0] == "KEY=hm_codex"


def test_hivemind_as_auto_picks_the_longest_mapped_folder(tmp_path: Path) -> None:
    env, config = _profiles(tmp_path, "codex")
    _key_file(config / "codex.client.env", "hm_client")
    _key_file(config / "codex.secret.env", "hm_secret")
    (config / "profiles").write_text(
        "# folder profile\n~/work client\n~/work/acme secret\n\n~/work/acme2-not-a-prefix nope\n"
    )
    for folder, key in (
        ("work/acme/src", "hm_secret"),
        ("work/acme", "hm_secret"),
        ("work/acme2", "hm_client"),
        ("elsewhere", "hm_inherited"),
    ):
        (tmp_path / folder).mkdir(parents=True, exist_ok=True)
        out = _switch(env, tmp_path / folder, "auto", "codex")
        assert out.stdout.splitlines()[0] == f"KEY={key}", folder


def test_hivemind_as_refuses_unsafe_profiles(tmp_path: Path) -> None:
    env, config = _profiles(tmp_path, "codex")
    _key_file(config / "codex.client.env", "hm_client")
    (config / "codex.client.env").chmod(0o640)
    out = _switch(env, tmp_path, "client", "codex", check=False)
    assert out.returncode == 2 and "chmod 600" in out.stderr and out.stdout == ""
    for name in ("../client", ".hidden", "a b"):
        out = _switch(env, tmp_path, name, "codex", check=False)
        assert out.returncode == 2 and "profile name" in out.stderr
    out = _switch(env, tmp_path, "missing", "codex", check=False)
    assert out.returncode == 2 and "no profile file" in out.stderr


def test_hivemind_as_lists_profile_names_only(tmp_path: Path) -> None:
    env, config = _profiles(tmp_path)
    _key_file(config / "codex.client.env", "hm_client")
    _key_file(config / "codex.env", "hm_codex")
    (config / "claude.client.json").write_text('{"env": {"HIVEMIND_API_KEY": "hm_c"}}')
    out = _switch(env, tmp_path, "--list")
    assert out.stdout.splitlines() == ["client (claude)", "client (codex)", "default (codex)"]
    assert "hm_" not in out.stdout


@pytest.mark.parametrize("harness", ["junie", "agy"])
def test_hivemind_as_refuses_harnesses_that_keep_the_key_in_their_own_config(
    tmp_path: Path, harness: str
) -> None:
    env, _ = _profiles(tmp_path, harness)
    out = _switch(env, tmp_path, "client", harness, check=False)
    assert out.returncode == 2 and out.stdout == "" and "hivemind-switch" in out.stderr


def test_hivemind_as_refuses_dsh_where_a_project_env_would_override_it(tmp_path: Path) -> None:
    """DSH's resolver ignores the environment when the folder's .env names
    HIVEMIND_* and falls back to ~/.dsh/.env, the default agent."""
    env, config = _profiles(tmp_path, "dsh")
    _key_file(config / "dsh.client.env", "hm_client")
    assert _switch(env, tmp_path, "client", "dsh").stdout.splitlines()[0] == "KEY=hm_client"
    (tmp_path / ".env").write_text("export HIVEMIND_MCP_URL=https://elsewhere/mcp\n")
    out = _switch(env, tmp_path, "client", "dsh", check=False)
    assert out.returncode == 2 and "~/.dsh/.env" in out.stderr


def test_the_switch_skill_keeps_keys_and_fleets_apart() -> None:
    flat = " ".join(SWITCH_SKILL.split())
    assert "Never read, print or copy a key" in flat
    assert "A switch is always a new session" in flat
    assert "Carry nothing across" in flat
    assert "never open a profile file" in flat


@pytest.mark.parametrize(
    ("args", "active", "checked"),
    [
        ((), None, ".hermes"),
        (("-p", "Work"), None, ".hermes/profiles/work"),
        (("chat", "--profile=ops"), None, ".hermes/profiles/ops"),
        ((), "ops", ".hermes/profiles/ops"),
        (("-p", "default"), "ops", ".hermes"),
    ],
)
def test_launcher_checks_the_hermes_profile_the_session_runs_in(
    tmp_path: Path, args: tuple[str, ...], active: str | None, checked: str
) -> None:
    """Hermes runs from the profile named by -p/--profile, else the one
    `hermes profile use` saved; its config and .env are checked there."""
    env = _fake_harness(tmp_path, "hermes")
    root = tmp_path / ".hermes"
    root.mkdir()
    if active:
        (root / "active_profile").write_text(f"{active}\n")
    out = subprocess.run(
        ["sh", str(LAUNCHER), "hermes", *args],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert f"{tmp_path}/{checked}/config.yaml does not gate" in out.stderr
    lines = out.stdout.splitlines()
    assert lines[len(lines) - len(args) :] == list(args)  # passed through unchanged


def test_launcher_accepts_a_gate_in_the_root_hermes_config_for_a_profile(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "hermes")
    profile = tmp_path / ".hermes" / "profiles" / "ops"
    profile.mkdir(parents=True)
    (tmp_path / ".hermes" / "config.yaml").write_text("enabled: ${HIVEMIND_ENABLED}\n")
    (profile / ".env").write_text("HIVEMIND_ENABLED=true\n")
    out = subprocess.run(
        ["sh", str(LAUNCHER), "hermes", "-p", "ops"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "does not gate" not in out.stderr
    assert f"{profile}/.env sets HIVEMIND_ENABLED" in out.stderr


@pytest.mark.parametrize("editor", ["code", "codium"])
def test_launcher_empties_the_key_and_url_for_a_code_editor(tmp_path: Path, editor: str) -> None:
    """The Cline and Kilo Code extensions read the editor's environment.
    Empty, not unset, so a running editor's own values are replaced too."""
    env = _fake_harness(tmp_path, editor)
    (tmp_path / "bin" / editor).write_text(
        "#!/bin/sh\n"
        "u=${HIVEMIND_MCP_URL-unset}; k=${HIVEMIND_API_KEY-unset}\n"
        'printf "%s\\n" "URL=${u:-set-empty}" "KEY=${k:-set-empty}" '
        '"INCOGNITO=$HIVEMIND_INCOGNITO" "$@"\n'
    )
    env.update(HIVEMIND_MCP_URL="https://hm.example/mcp", HIVEMIND_API_KEY="hm_x")
    out = subprocess.run(
        ["sh", str(LAUNCHER), editor, "."],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.splitlines() == ["URL=set-empty", "KEY=set-empty", "INCOGNITO=1", "."]
    assert "already open" in out.stderr


def test_launcher_starts_pi_with_the_key_and_url_unset(tmp_path: Path) -> None:
    """Pi connects through its built-in MCP client: the launcher only unsets
    the key and URL (the extension registers no server in an incognito
    session), passes the arguments through unchanged and needs no python3."""
    env = _fake_harness(tmp_path, "pi")
    script = tmp_path / "bin" / "pi"
    script.write_text(
        '#!/bin/sh\nprintf "%s\\n" "KEY=${HIVEMIND_API_KEY:-unset}" '
        '"URL=${HIVEMIND_MCP_URL:-unset}" "INCOGNITO=$HIVEMIND_INCOGNITO" "$@"\n'
    )
    env.update(HIVEMIND_API_KEY="hm_x", HIVEMIND_MCP_URL="https://hm.example/mcp")
    lines = _launch(tmp_path, env, "pi", "-p", "hi")
    assert lines == ["KEY=unset", "URL=unset", "INCOGNITO=1", "-p", "hi"]


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
        PLUGIN / "package.json",  # the update skill's drift check reads the installed version here
        PLUGIN / "skills" / "hivemind" / "SKILL.md",
        PLUGIN / "skills" / "hivemind-setup" / "SKILL.md",
        PLUGIN / "skills" / "hivemind-update" / "scripts" / "hivemind-drift.sh",
        PLUGIN / "bin" / "hivemind-as",  # the hivemind-switch skill looks for it beside the skills
        PLUGIN / "bin" / "hivemind-incognito",
        *SETUP_REFS.glob("*.md"),
        *UPDATE_REFS.glob("*.md"),
    ]
    needed += [patch.parent / rel for rel in re.findall(r"name: (\./\S+)", patch.read_text())]
    for path in needed:
        assert path.exists(), path
        assert is_shipped(path), f"{path.relative_to(ROOT)} is missing from package.json files"
