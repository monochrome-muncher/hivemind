"""The Claude Code and Codex Stop hook and the post-compaction line (report §7 row 10)."""

import json
import os
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "hivemind"
SCRIPTS = PLUGIN / "skills" / "hivemind-setup" / "scripts"
STOP = SCRIPTS / "hivemind-stop.sh"
START = SCRIPTS / "hivemind-session-start.sh"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.update(TMPDIR=str(tmp_path / "tmp"), HIVEMIND_API_KEY="hm_key", **extra)
    (tmp_path / "tmp").mkdir(exist_ok=True)
    return env


def _transcript(tmp_path: Path, tool_uses: int, *names: str) -> Path:
    path = tmp_path / "t.jsonl"
    lines = [
        json.dumps({"message": {"content": [{"type": "tool_use", "name": n, "input": {}}]}})
        for n in ("Bash",) * tool_uses + names
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def _stop(
    tmp_path: Path,
    transcript: Path | None,
    env: dict[str, str] | None = None,
    session_id: str = "s1",
    **fields: object,
) -> subprocess.CompletedProcess[str]:
    payload: dict[str, object] = {"session_id": session_id, "hook_event_name": "Stop"}
    if transcript is not None:
        payload["transcript_path"] = str(transcript)
    payload.update(fields)
    return subprocess.run(
        ["sh", str(STOP)],
        input=json.dumps(payload),
        env=env or _env(tmp_path),
        capture_output=True,
        text=True,
    )


def test_hooks_json_registers_the_stop_script() -> None:
    [stop] = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())["hooks"]["Stop"]
    [hook] = stop["hooks"]
    assert "skills/hivemind-setup/scripts/hivemind-stop.sh" in hook["command"]


def test_a_substantial_session_is_blocked_once_with_a_reason(tmp_path: Path) -> None:
    t = _transcript(tmp_path, 25)
    first = _stop(tmp_path, t)
    assert first.returncode == 0
    out = json.loads(first.stdout)
    assert out["decision"] == "block"
    assert "hive_write" in out["reason"] and "hive_feedback" in out["reason"]
    again = _stop(tmp_path, t)
    assert (again.returncode, again.stdout) == (0, "")
    other = _stop(tmp_path, t, session_id="s2")
    assert json.loads(other.stdout)["decision"] == "block"


def _codex_transcript(tmp_path: Path, calls: int, *mcp_names: str) -> Path:
    """A Codex rollout as Codex 0.162 writes it: each call a compact
    `response_item` of type `function_call` with its output in a separate
    `function_call_output` item; an MCP call carries the server as
    `namespace`."""
    path = tmp_path / "rollout.jsonl"
    items: list[dict[str, object]] = []
    for i in range(calls):
        items.append({"type": "function_call", "name": "exec_command", "call_id": f"c{i}"})
        items.append({"type": "function_call_output", "call_id": f"c{i}", "output": "ok"})
    for name in mcp_names:
        items.append({"type": "function_call", "name": name, "namespace": "mcp__hivemind"})
    lines = [
        json.dumps({"type": "response_item", "payload": item}, separators=(",", ":"))
        for item in items
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_substantial_codex_session_is_blocked(tmp_path: Path) -> None:
    out = _stop(tmp_path, _codex_transcript(tmp_path, 25))
    assert json.loads(out.stdout)["decision"] == "block"


def test_codex_call_outputs_are_not_counted_as_calls(tmp_path: Path) -> None:
    out = _stop(tmp_path, _codex_transcript(tmp_path, 15))
    assert (out.returncode, out.stdout) == (0, "")


def test_a_codex_session_that_already_wrote_is_not_blocked(tmp_path: Path) -> None:
    out = _stop(tmp_path, _codex_transcript(tmp_path, 25, "hive_write"))
    assert (out.returncode, out.stdout) == (0, "")


def test_a_short_session_is_not_blocked(tmp_path: Path) -> None:
    out = _stop(tmp_path, _transcript(tmp_path, 5))
    assert (out.returncode, out.stdout) == (0, "")


@pytest.mark.parametrize(
    "tool", ["mcp__hivemind__hive_write", "mcp__plugin_hivemind_hivemind__hive_feedback"]
)
def test_a_session_that_already_wrote_or_rated_is_not_blocked(tmp_path: Path, tool: str) -> None:
    out = _stop(tmp_path, _transcript(tmp_path, 30, tool))
    assert (out.returncode, out.stdout) == (0, "")


def test_searching_alone_does_not_count_as_contributing(tmp_path: Path) -> None:
    t = _transcript(tmp_path, 30, "mcp__hivemind__hive_search")
    assert json.loads(_stop(tmp_path, t).stdout)["decision"] == "block"


def test_stop_hook_active_never_blocks(tmp_path: Path) -> None:
    out = _stop(tmp_path, _transcript(tmp_path, 30), stop_hook_active=True)
    assert (out.returncode, out.stdout) == (0, "")


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_incognito_never_blocks(tmp_path: Path, value: str) -> None:
    env = _env(tmp_path, HIVEMIND_INCOGNITO=value)
    out = _stop(tmp_path, _transcript(tmp_path, 30), env)
    assert (out.returncode, out.stdout) == (0, "")


def test_an_unconfigured_session_never_blocks(tmp_path: Path) -> None:
    env = _env(tmp_path)
    del env["HIVEMIND_API_KEY"]
    out = _stop(tmp_path, _transcript(tmp_path, 30), env)
    assert (out.returncode, out.stdout) == (0, "")


def test_the_threshold_is_tunable(tmp_path: Path) -> None:
    env = _env(tmp_path, HIVEMIND_STOP_MIN_TOOLS="3")
    assert json.loads(_stop(tmp_path, _transcript(tmp_path, 4), env).stdout)["decision"] == "block"


@pytest.mark.parametrize("stdin", ["", "not json", "{", '{"session_id": "s"}'])
def test_garbage_or_missing_input_fails_open(tmp_path: Path, stdin: str) -> None:
    out = subprocess.run(
        ["sh", str(STOP)], input=stdin, env=_env(tmp_path), capture_output=True, text=True
    )
    assert (out.returncode, out.stdout) == (0, "")


def test_a_missing_transcript_fails_open(tmp_path: Path) -> None:
    out = _stop(tmp_path, tmp_path / "nope.jsonl")
    assert (out.returncode, out.stdout) == (0, "")


def test_an_unwritable_state_dir_fails_open(tmp_path: Path) -> None:
    env = _env(tmp_path)
    blocker = tmp_path / "file"
    blocker.write_text("")
    env["TMPDIR"] = str(blocker)  # a file, so mkdir -p under it fails
    out = _stop(tmp_path, _transcript(tmp_path, 30), env)
    assert (out.returncode, out.stdout) == (0, "")


def _start(tmp_path: Path, stdin: str, **extra: str) -> str:
    out = subprocess.run(
        ["sh", str(START)],
        input=stdin,
        env=_env(tmp_path, **extra),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]


def test_only_the_compact_source_gets_the_post_compaction_line(tmp_path: Path) -> None:
    compact = _start(tmp_path, '{"hook_event_name":"SessionStart","source":"compact"}')
    assert "compacted" in compact and "hive_write" in compact and "hive_feedback" in compact
    for source in ("startup", "resume", "clear", "fork"):
        text = _start(tmp_path, json.dumps({"source": source}))
        assert "compacted" not in text
    assert "compacted" not in _start(tmp_path, "")


def test_incognito_compact_stays_incognito(tmp_path: Path) -> None:
    text = _start(tmp_path, '{"source":"compact"}', HIVEMIND_INCOGNITO="1")
    assert "incognito" in text and "compacted" not in text
