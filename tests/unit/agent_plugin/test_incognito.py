"""Incognito sessions (ADR 0035) across the plugin's harnesses."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.unit.agent_plugin.common import (
    HOOK,
    INCOGNITO_TEXT,
    LAUNCHER,
    PLUGIN,
    SETUP_REFS,
    STAY_AWARE_CORE,
    _fake_harness,
    _launch,
    _load_hermes_plugin,
    _pi_extension_text,
)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_the_hook_goes_incognito(value: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.update(HIVEMIND_INCOGNITO=value, HIVEMIND_API_KEY="hm_secret_value")
    out = subprocess.run(
        ["sh", str(HOOK)],
        env=env,
        stdin=subprocess.DEVNULL,  # the hook reads stdin when it is not a TTY
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] == INCOGNITO_TEXT
    assert "hm_secret_value" not in out.stdout


def test_the_hook_ignores_a_false_incognito_value() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env["HIVEMIND_INCOGNITO"] = "0"
    out = subprocess.run(
        ["sh", str(HOOK)],
        env=env,
        stdin=subprocess.DEVNULL,  # the hook reads stdin when it is not a TTY
        capture_output=True,
        text=True,
        check=True,
    )
    assert STAY_AWARE_CORE in out.stdout


def test_the_hermes_section_goes_incognito(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEMIND_INCOGNITO", "1")
    assert _load_hermes_plugin()._hivemind_section() == INCOGNITO_TEXT


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("with_key", [True, False], ids=["key-set", "no-key"])
def test_the_pi_section_goes_incognito(with_key: bool) -> None:
    assert _pi_extension_text(with_key=with_key, HIVEMIND_INCOGNITO="true") == INCOGNITO_TEXT


def test_launcher_denies_the_server_in_claude_code(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "claude")
    env["HIVEMIND_MCP_URL"] = "https://hm.example/mcp"
    lines = _launch(tmp_path, env, "claude", "--resume")
    assert lines[0] == "INCOGNITO=1"
    settings = json.loads(lines[lines.index("--settings") + 1])
    assert {"serverUrl": "https://hm.example/mcp"} in settings["deniedMcpServers"]
    # A plugin's server registers under "plugin:<plugin>:<server>".
    assert {"serverName": "hivemind"} in settings["deniedMcpServers"]
    assert {"serverName": "plugin:hivemind:hivemind"} in settings["deniedMcpServers"]
    assert lines[-1] == "--resume"


def _claude_printer(tmp_path: Path) -> dict[str, str]:
    env = _fake_harness(tmp_path, "claude")
    (tmp_path / "bin" / "claude").write_text(
        '#!/bin/sh\nprintf "%s\\n" "KEY=${HIVEMIND_API_KEY:-unset}" "URL=${HIVEMIND_MCP_URL:-unset}" "$@"\n'
    )
    return env


def test_launcher_denies_every_hivemind_call_in_antigravity(tmp_path: Path) -> None:
    """Antigravity has no per-session switch for a plugin; a session-only
    permission rule refuses every call to the plugin's server."""
    env = _fake_harness(tmp_path, "agy")
    lines = _launch(tmp_path, env, "agy", "-p", "hi")
    assert lines[0] == "INCOGNITO=1"
    settings = json.loads(lines[lines.index("--settings") + 1])
    assert settings == {"permissions": {"deny": ["mcp(hivemind_hivemind/*)"]}}
    assert lines[-2:] == ["-p", "hi"]


def test_launcher_json_escapes_the_claude_url_and_unsets_the_key(tmp_path: Path) -> None:
    env = _claude_printer(tmp_path)
    env.update(HIVEMIND_MCP_URL='https://hm.example/mcp?x="y"\\z', HIVEMIND_API_KEY="hm_x")
    lines = _launch(tmp_path, env, "claude")
    assert lines[:2] == ["KEY=unset", "URL=unset"]
    settings = json.loads(lines[lines.index("--settings") + 1])  # valid JSON despite the quote
    assert {"serverUrl": 'https://hm.example/mcp?x="y"\\z'} in settings["deniedMcpServers"]


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is not installed")
def test_launcher_finds_the_claude_url_in_settings_json_env(tmp_path: Path) -> None:
    """The setup skill offers ~/.claude/settings.json "env" as an equal home
    for the URL; the launcher must not depend on the launching shell."""
    env = _claude_printer(tmp_path)
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"env": {"HIVEMIND_MCP_URL": "https://from-settings/mcp"}})
    )
    lines = _launch(tmp_path, env, "claude")
    settings = json.loads(lines[lines.index("--settings") + 1])
    assert {"serverUrl": "https://from-settings/mcp"} in settings["deniedMcpServers"]


def test_launcher_survives_an_unset_home(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "codex")
    del env["HOME"], env["CODEX_HOME"]
    out = subprocess.run(
        ["sh", str(LAUNCHER), "codex"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr  # no "HOME: parameter not set" abort


def test_launcher_finds_a_project_level_or_spaced_codex_table(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "codex")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('[ mcp_servers.hivemind ]\nurl = "x"\n')
    lines = _launch(tmp_path, env, "codex")
    assert lines[lines.index("-c") + 1] == "mcp_servers.hivemind.enabled=false"


def test_launcher_disables_codex_only_when_configured(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "codex")
    assert "-c" not in _launch(tmp_path, env, "codex")  # no config: no invalid override
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "config.toml").write_text('[mcp_servers.hivemind]\nurl = "x"\n')
    lines = _launch(tmp_path, env, "codex", "task")
    assert lines[lines.index("-c") + 1] == "mcp_servers.hivemind.enabled=false"


def test_launcher_unsets_the_codex_key_even_without_a_hivemind_entry(tmp_path: Path) -> None:
    """A trusted project's .codex/config.toml can name another server that
    reads HIVEMIND_API_KEY; an incognito session never needs the key."""
    env = _fake_harness(tmp_path, "codex")
    (tmp_path / "bin" / "codex").write_text(
        '#!/bin/sh\nprintf "%s\\n" "KEY=${HIVEMIND_API_KEY:-unset}" "$@"\n'
    )
    env.update(HIVEMIND_API_KEY="hm_x")
    assert _launch(tmp_path, env, "codex", "task") == ["KEY=unset", "task"]


def test_launcher_turns_hermes_off_through_its_config_variable(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "hermes")
    assert "ENABLED=false" in _launch(tmp_path, env, "hermes")


def _gateway_state(home: Path, pid: int, state: str = "running") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "gateway_state.json").write_text(
        json.dumps({"pid": pid, "kind": "hermes-gateway", "gateway_state": state})
    )


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _run_launcher(
    tmp_path: Path, env: dict[str, str], *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(LAUNCHER), *args], env=env, cwd=tmp_path, capture_output=True, text=True
    )


def test_launcher_refuses_hermes_while_a_gateway_is_running(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "hermes")
    _gateway_state(tmp_path / ".hermes", os.getpid())
    out = _run_launcher(tmp_path, env, "hermes", "chat")
    assert out.returncode == 1
    assert out.stdout == ""
    assert f"a Hermes gateway is running for this profile (pid {os.getpid()})" in out.stderr
    assert "hermes gateway stop" in out.stderr


def test_launcher_refuses_hermes_for_the_named_profiles_gateway(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "hermes")
    _gateway_state(tmp_path / ".hermes" / "profiles" / "work", os.getpid())
    assert _run_launcher(tmp_path, env, "hermes", "-p", "Work").returncode == 1
    # the root's gateway is another home: a different profile is not blocked
    assert _run_launcher(tmp_path, env, "hermes", "-p", "other").returncode == 0
    assert _run_launcher(tmp_path, env, "hermes").returncode == 0


def test_launcher_refuses_hermes_for_the_root_gateway(tmp_path: Path) -> None:
    env = _fake_harness(tmp_path, "hermes")
    _gateway_state(tmp_path / ".hermes", os.getpid())
    assert _run_launcher(tmp_path, env, "hermes", "-p", "work").returncode == 0
    assert _run_launcher(tmp_path, env, "hermes", "-p", "default").returncode == 1


@pytest.mark.parametrize("case", ["no file", "dead pid", "not running", "bad file"])
def test_launcher_runs_hermes_when_no_live_gateway_is_up(tmp_path: Path, case: str) -> None:
    env = _fake_harness(tmp_path, "hermes")
    home = tmp_path / ".hermes"
    if case == "dead pid":
        _gateway_state(home, _dead_pid())
    elif case == "not running":
        _gateway_state(home, os.getpid(), "stopped")
    elif case == "bad file":
        home.mkdir()
        (home / "gateway_state.json").write_text("not json")
    assert "ENABLED=false" in _launch(tmp_path, env, "hermes")


def test_launcher_warns_when_the_hermes_env_file_would_override_it(tmp_path: Path) -> None:
    """Hermes loads ~/.hermes/.env over the environment, so a
    HIVEMIND_ENABLED there beats the launcher's false; the docs must not
    tell users to put it there."""
    env = _fake_harness(tmp_path, "hermes")
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / ".env").write_text("HIVEMIND_ENABLED=true\n")
    out = subprocess.run(
        ["sh", str(LAUNCHER), "hermes"], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    assert "sets HIVEMIND_ENABLED, which overrides this launcher" in out.stderr
    for text in (
        (SETUP_REFS / "hermes.md").read_text(),
        (PLUGIN / "README.md").read_text(),
        (PLUGIN / "DEVELOPING.md").read_text(),
    ):
        flat = " ".join(text.split())
        assert "HIVEMIND_ENABLED=true` in the Hermes env" not in flat
        assert "HIVEMIND_ENABLED=true` in `~/.hermes/.env`" not in flat
