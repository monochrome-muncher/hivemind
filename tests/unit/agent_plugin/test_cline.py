"""Cline: the file hooks (stay-aware line and end-of-task nudge)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.unit.agent_plugin.common import (
    CLINE_SHORT_LINE,
    INCOGNITO_TEXT,
    PLUGIN,
    STAY_AWARE_CORE,
    STOP_REMINDER_TEXT,
)

CLINE_HOOKS = PLUGIN / "cline"


def _cline_hook(name: str, payload: str, tmp_path: Path, **env_overrides: str) -> str:
    """Run one Cline hook script with a payload on stdin and return its line.

    The hooks keep their counter in a private per-user folder; they prefer
    $XDG_RUNTIME_DIR when it is a real directory of ours. Point it at a fresh
    per-test directory so state is isolated between tests but stable across
    the several hook calls a test makes.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    xdg = tmp_path / "xdg"
    xdg.mkdir(exist_ok=True)
    env["XDG_RUNTIME_DIR"] = str(xdg)
    env["TMPDIR"] = str(tmp_path)  # fallback if the XDG dir is refused
    env.update(env_overrides)
    out = subprocess.run(
        ["sh", str(CLINE_HOOKS / name)],
        env=env,
        input=payload,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"HIVEMIND_API_KEY": "hm_secret_value"}, "Hivemind is configured."),
        ({}, "probably not connected"),
        ({"HIVEMIND_INCOGNITO": "1", "HIVEMIND_API_KEY": "hm_secret_value"}, INCOGNITO_TEXT),
    ],
)
def test_cline_user_prompt_submit_carries_the_stay_aware_line(
    tmp_path: Path, env: dict[str, str], expected: str
) -> None:
    line = _cline_hook("UserPromptSubmit", "{}", tmp_path, **env)
    hook = json.loads(line)
    assert hook["cancel"] is False
    assert expected in hook["contextModification"]
    if expected != INCOGNITO_TEXT:
        assert hook["contextModification"].startswith(STAY_AWARE_CORE)
    assert "hm_secret_value" not in line


def _cline_prompt(tmp_path: Path, task: str = "t1", **extra: str) -> str:
    env = {"HIVEMIND_API_KEY": "hm_secret_value", **extra}
    line = _cline_hook("UserPromptSubmit", json.dumps({"taskId": task}), tmp_path, **env)
    text: str = json.loads(line)["contextModification"]
    return text


def test_cline_user_prompt_submit_is_full_on_a_tasks_first_prompt_then_short(
    tmp_path: Path,
) -> None:
    _cline_hook("TaskStart", json.dumps({"taskId": "t1"}), tmp_path)
    assert _cline_prompt(tmp_path).startswith(STAY_AWARE_CORE)
    assert _cline_prompt(tmp_path) == CLINE_SHORT_LINE
    assert _cline_prompt(tmp_path) == CLINE_SHORT_LINE
    # Another task, even without a TaskStart, starts with the full line.
    assert _cline_prompt(tmp_path, task="t2").startswith(STAY_AWARE_CORE)
    # A restarted task (TaskStart again) gets the full line again.
    _cline_hook("TaskStart", json.dumps({"taskId": "t1"}), tmp_path)
    assert _cline_prompt(tmp_path).startswith(STAY_AWARE_CORE)
    assert _cline_prompt(tmp_path) == CLINE_SHORT_LINE


def test_cline_user_prompt_submit_stays_full_when_it_cannot_track_the_task(
    tmp_path: Path,
) -> None:
    # No task id, and no key (the line then carries the setup warning).
    for _ in range(2):
        no_task = _cline_hook(
            "UserPromptSubmit", "{}", tmp_path, HIVEMIND_API_KEY="hm_secret_value"
        )
        assert json.loads(no_task)["contextModification"].startswith(STAY_AWARE_CORE)
        no_key = _cline_hook("UserPromptSubmit", json.dumps({"taskId": "k"}), tmp_path)
        assert "probably not connected" in json.loads(no_key)["contextModification"]


def test_cline_state_file_keeps_the_prompt_flag_and_old_files_still_work(
    tmp_path: Path,
) -> None:
    xdg = tmp_path / "xdg"

    def tool(task: str) -> None:
        _cline_hook(
            "PostToolUse",
            json.dumps({"taskId": task, "postToolUse": {"toolName": "hive_search"}}),
            tmp_path,
        )

    _cline_hook("TaskStart", json.dumps({"taskId": "t1"}), tmp_path)
    state = xdg / "hivemind-cline" / "t1"
    assert state.read_text().split() == ["0"] * 6
    assert _cline_prompt(tmp_path).startswith(STAY_AWARE_CORE)
    tool("t1")
    # The counter moves, the "full line sent" flag survives.
    assert state.read_text().split() == ["1", "0", "0", "0", "1", "1"]
    assert _cline_prompt(tmp_path) == CLINE_SHORT_LINE

    # Old state files (3 and 5 fields) are read: the full line once, the
    # counters kept, then the six-field form.
    for old, kept in (("4 1 0\n", "4 1 0 0 0 1"), ("4 1 0 0 1\n", "4 1 0 0 1 1")):
        state.write_text(old)
        assert _cline_prompt(tmp_path).startswith(STAY_AWARE_CORE)
        assert state.read_text().split() == kept.split()
        assert _cline_prompt(tmp_path) == CLINE_SHORT_LINE
    # PostToolUse reads an old file too and writes the six-field form.
    state.write_text("4 1 0\n")
    tool("t1")
    assert state.read_text().split() == ["5", "1", "0", "0", "1", "0"]


def test_cline_post_tool_use_nudges_once_at_the_threshold(tmp_path: Path) -> None:
    def calls(n: int, task: str = "t1", tool: str = "bash", **extra: str) -> list[str]:
        return [
            _cline_hook(
                "PostToolUse",
                json.dumps({"taskId": task, "postToolUse": {"toolName": tool}, **extra}),
                tmp_path,
                HIVEMIND_API_KEY="hm_secret_value",
                HIVEMIND_STOP_MIN_TOOLS="3",
            )
            for _ in range(n)
        ]

    _cline_hook("TaskStart", json.dumps({"taskId": "t1"}), tmp_path)
    out = calls(3)
    # The nudge goes out on the call that reaches the threshold, once.
    assert json.loads(out[2])["contextModification"] == STOP_REMINDER_TEXT
    quiet = json.loads(calls(1)[0])
    assert quiet["cancel"] is False and "contextModification" not in quiet
    # A contribution keeps it quiet.
    _cline_hook("TaskStart", json.dumps({"taskId": "t2"}), tmp_path)
    calls(2, task="t2")
    assert "contextModification" not in json.loads(
        calls(1, task="t2", tool="hivemind__hive_write")[0]
    )
    assert "contextModification" not in json.loads(calls(1, task="t2")[0])
    # A write after reading entries, with no rating, still nudges once; a
    # rating keeps it quiet.
    _cline_hook("TaskStart", json.dumps({"taskId": "t3"}), tmp_path)
    calls(1, task="t3", tool="hivemind__hive_get")
    calls(1, task="t3", tool="hivemind__hive_write")
    assert json.loads(calls(1, task="t3")[0])["contextModification"] == STOP_REMINDER_TEXT
    assert "contextModification" not in json.loads(calls(1, task="t3")[0])
    _cline_hook("TaskStart", json.dumps({"taskId": "t4"}), tmp_path)
    calls(1, task="t4", tool="hivemind__hive_search")
    calls(1, task="t4", tool="hivemind__hive_feedback")
    assert "contextModification" not in json.loads(calls(1, task="t4")[0])
    # A subagent is never counted and never nudged: the CLI hook runner marks
    # one with a top-level parent_agent_id, the other runner with
    # sessionContext.parentAgentId (and the camelCase twin at the top level).
    for i, marker in enumerate(
        (
            {"parent_agent_id": "t1"},
            {"parentAgentId": "t1"},
            {"sessionContext": {"parentAgentId": "t1"}},
        )
    ):
        sub = calls(5, task=f"sub{i}", **marker)
        assert all("contextModification" not in json.loads(x) for x in sub)


def test_cline_post_tool_use_reads_the_real_tool_name_field(tmp_path: Path) -> None:
    """Cline sends postToolUse.toolName (camelCase); a hive_write by that name
    counts as a contribution and keeps the nudge quiet."""

    def run(task: str, tool: str, n: int, shape: str = "toolName", **extra: object) -> list[str]:
        return [
            _cline_hook(
                "PostToolUse",
                json.dumps({"taskId": task, "postToolUse": {shape: tool}, **extra}),
                tmp_path,
                HIVEMIND_API_KEY="hm_secret_value",
                HIVEMIND_STOP_MIN_TOOLS="3",
            )
            for _ in range(n)
        ]

    # Control: the same calls without a write do nudge.
    assert "contextModification" in json.loads(run("w0", "bash", 3)[2])
    # A write via toolName prevents it.
    out = run("w1", "bash", 1) + run("w1", "mcp__hivemind__hive_write", 1) + run("w1", "bash", 3)
    assert all("contextModification" not in json.loads(x) for x in out)
    # The legacy snake_case name still works.
    out = run("w2", "mcp__hivemind__hive_write", 1, shape="tool_name") + run("w2", "bash", 4)
    assert all("contextModification" not in json.loads(x) for x in out)
    # A JSON null parentAgentId on a main task does not mark a subagent.
    out = run("w3", "bash", 3, sessionContext={"parentAgentId": None})
    assert "contextModification" in json.loads(out[2])


def test_cline_nudge_is_silent_when_it_cannot_work(tmp_path: Path) -> None:
    def run(n: int, **env: str) -> list[str]:
        return [
            _cline_hook(
                "PostToolUse",
                json.dumps({"taskId": "t", "postToolUse": {"toolName": "bash"}}),
                tmp_path,
                HIVEMIND_STOP_MIN_TOOLS="2",
                **env,
            )
            for _ in range(n)
        ]

    incognito = run(3, HIVEMIND_INCOGNITO="1", HIVEMIND_API_KEY="hm_secret_value")
    assert all("contextModification" not in json.loads(x) for x in incognito)
    no_key = run(3)
    assert all("contextModification" not in json.loads(x) for x in no_key)


def test_cline_task_start_names_the_same_state_folder_on_both_platforms() -> None:
    """TaskStart carries no text (the .ps1 twins' texts are pinned by
    ``REMINDER_COPIES``); only the two platforms' state-folder names must
    agree (a private per-user folder, as in hivemind-stop.sh)."""
    assert "hivemind-cline" in (CLINE_HOOKS / "TaskStart").read_text()
    assert "hivemind-cline" in (CLINE_HOOKS / "TaskStart.ps1").read_text()


def test_cline_ps1_hooks_read_the_payload_and_numbers_like_the_sh_twins() -> None:
    """There is no PowerShell here, so this is a static check. The sh twins
    allow whitespace around the colon (Cline's serializers differ), reject a
    negative HIVEMIND_STOP_MIN_TOOLS, and treat a non-numeric state file as
    zeros; the .ps1 files must do the same without throwing."""
    for name in ("PostToolUse.ps1", "TaskStart.ps1", "UserPromptSubmit.ps1"):
        text = (CLINE_HOOKS / name).read_text()
        assert '":"(' not in text, name
        assert '"\\s*:\\s*"([^"]*)"' in text, name
    post = (CLINE_HOOKS / "PostToolUse.ps1").read_text()
    assert "[int]$" not in post  # a bare cast throws on a bad state file
    assert "[int]::TryParse($env:HIVEMIND_STOP_MIN_TOOLS, [ref]$parsed) -and $parsed -ge 0" in post
    assert "$n -ge 0" in post


def test_cline_hooks_refuse_a_symlinked_state_folder(tmp_path: Path) -> None:
    """The counter must not be written through a pre-created symlink: on a
    multi-user host another local user could plant `hivemind-cline` as a
    symlink and have the hook truncate a file they own. A symlinked (or
    non-owned) state folder is refused and the hook fails open."""
    target = tmp_path / "victim"
    target.write_text("precious")
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    link = xdg / "hivemind-cline"
    link.symlink_to(target)
    payload = json.dumps({"taskId": "t1", "postToolUse": {"toolName": "bash"}})
    for name in ("TaskStart", "PostToolUse"):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
        env["XDG_RUNTIME_DIR"] = str(xdg)
        env["HIVEMIND_API_KEY"] = "hm_secret_value"
        out = subprocess.run(
            ["sh", str(CLINE_HOOKS / name)],
            env=env,
            input=payload,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(out.stdout.strip()) == {"cancel": False}
    # The symlink target was left untouched (not truncated to the counter).
    assert target.read_text() == "precious"


def test_cline_hooks_write_the_counter_into_a_private_folder(tmp_path: Path) -> None:
    """The counter lands in a 0700 folder owned by the user, not a
    world-writable file in shared /tmp."""
    xdg = tmp_path / "xdg"
    xdg.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env["XDG_RUNTIME_DIR"] = str(xdg)
    env["HIVEMIND_API_KEY"] = "hm_secret_value"
    for _ in range(3):
        subprocess.run(
            ["sh", str(CLINE_HOOKS / "PostToolUse")],
            env=env,
            input=json.dumps({"taskId": "t1", "postToolUse": {"toolName": "bash"}}),
            capture_output=True,
            text=True,
            check=True,
        )
    folder = xdg / "hivemind-cline"
    assert folder.is_dir() and not folder.is_symlink()
    assert oct(folder.stat().st_mode & 0o777) == "0o700"
    counter = folder / "t1"
    assert counter.read_text().split() == ["3", "0", "0", "0", "0", "0"]
