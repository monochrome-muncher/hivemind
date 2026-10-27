"""The Claude Code and Codex Stop hook and the post-compaction line (report §7 row 10)."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "hivemind"
SCRIPTS = PLUGIN / "skills" / "hivemind-setup" / "scripts"
STOP = SCRIPTS / "hivemind-stop.sh"
START = SCRIPTS / "hivemind-session-start.sh"


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    env.pop("XDG_RUNTIME_DIR", None)  # the host's value must not leak in
    env.pop("COPILOT_CLI", None)  # nor the flat Copilot CLI output form
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


def _snapshot_transcript(tmp_path: Path, tool_uses: int, *names: str) -> Path:
    """Claude Code lists every tool definition in a prompt_snapshot attachment line."""
    path = _transcript(tmp_path, tool_uses, *names)
    tools = [
        {
            "name": f"mcp__plugin_hivemind_hivemind__{t}",
            "description": "d",
            "schema": {"type": "object", "properties": {}},
            "server": "plugin:hivemind:hivemind",
        }
        for t in ("hive_write", "hive_feedback")
    ]
    snapshot = {"type": "attachment", "attachment": {"type": "prompt_snapshot", "tools": tools}}
    path.write_text(json.dumps(snapshot) + "\n" + path.read_text())
    return path


def test_tool_definitions_in_a_prompt_snapshot_are_not_calls(tmp_path: Path) -> None:
    t = _snapshot_transcript(tmp_path, 25)
    assert json.loads(_stop(tmp_path, t).stdout)["decision"] == "block"


def test_a_real_call_after_a_prompt_snapshot_keeps_it_quiet(tmp_path: Path) -> None:
    t = _snapshot_transcript(tmp_path, 25, "mcp__plugin_hivemind_hivemind__hive_write")
    out = _stop(tmp_path, t)
    assert (out.returncode, out.stdout) == (0, "")


def test_a_definition_line_does_not_add_to_the_tool_count(tmp_path: Path) -> None:
    path = _transcript(tmp_path, 4)
    deferred = {"type": "deferred_tools_record", "tools": [{"type": "tool_use", "name": "x"}]}
    snapshot = {"type": "attachment", "attachment": {"type": "prompt_snapshot", "x": "tool_use"}}
    schema = {"type": "tool_use", "name": "y", "input_schema": {"type": "object"}}
    extra = [json.dumps(d) for d in (deferred, snapshot, schema)]
    path.write_text(path.read_text() + "\n".join(extra) + "\n")
    out = _stop(tmp_path, path, env=_env(tmp_path, HIVEMIND_STOP_MIN_TOOLS="5"))
    assert (out.returncode, out.stdout) == (0, "")


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


def _gemini_transcript(tmp_path: Path, calls: int, *names: str) -> Path:
    """A Gemini CLI 0.63 chat log: JSONL whose model message is written again
    each time a call progresses, so every call (and its functionResponse id)
    appears several times."""
    path = tmp_path / "session.jsonl"

    def call(i: int, name: str) -> dict[str, object]:
        cid = f"{name}__{name}_17917305_{i}"
        response = {"id": cid, "name": name, "response": {"output": "ok"}}
        return {"id": cid, "name": name, "args": {}, "result": [{"functionResponse": response}]}

    tool_calls = [call(i, "run_shell_command") for i in range(calls)]
    tool_calls += [call(calls + i, n) for i, n in enumerate(names)]
    record = {"id": "m1", "type": "gemini", "content": "", "toolCalls": tool_calls}
    lines = [json.dumps({"sessionId": "s1", "kind": "main"})]
    lines += [json.dumps(record, separators=(",", ":"))] * 3
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_substantial_gemini_session_is_blocked_counting_each_call_once(tmp_path: Path) -> None:
    assert (
        json.loads(_stop(tmp_path, _gemini_transcript(tmp_path, 20)).stdout)["decision"] == "block"
    )
    short = _stop(tmp_path, _gemini_transcript(tmp_path, 10), session_id="s2")
    assert (short.returncode, short.stdout) == (0, "")


def test_a_gemini_session_that_already_wrote_is_not_blocked(tmp_path: Path) -> None:
    out = _stop(tmp_path, _gemini_transcript(tmp_path, 25, "mcp_hivemind_hive_write"))
    assert (out.returncode, out.stdout) == (0, "")


def _copilot_transcript(tmp_path: Path, calls: int, *names: str) -> Path:
    """A Copilot CLI 1.0.95 `events.jsonl`: one `tool.execution_start` event
    per call (the call also appears in the `assistant.message` that asked
    for it, as a `toolRequests` entry carrying `name`). An MCP tool is
    named `<server>-<tool>`."""
    path = tmp_path / "events.jsonl"
    lines = [json.dumps({"type": "session.start", "data": {"sessionId": "s1"}})]
    for i, name in enumerate(["bash"] * calls + list(names)):
        request = {"toolCallId": f"call_{i}", "name": name, "arguments": {}, "type": "function"}
        lines.append(json.dumps({"type": "assistant.message", "data": {"toolRequests": [request]}}))
        start = {"toolCallId": f"call_{i}", "toolName": name, "arguments": {}}
        lines.append(json.dumps({"type": "tool.execution_start", "data": start}))
        done = {"toolCallId": f"call_{i}", "success": True, "result": {"content": "ok"}}
        lines.append(json.dumps({"type": "tool.execution_complete", "data": done}))
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_substantial_copilot_session_is_blocked_counting_each_call_once(tmp_path: Path) -> None:
    out = _stop(tmp_path, _copilot_transcript(tmp_path, 20))
    assert json.loads(out.stdout)["decision"] == "block"
    short = _stop(tmp_path, _copilot_transcript(tmp_path, 19), session_id="s2")
    assert (short.returncode, short.stdout) == (0, "")


@pytest.mark.parametrize("tool", ["hivemind-hive_write", "hivemind-hive_feedback"])
def test_a_copilot_session_that_already_wrote_or_rated_is_not_blocked(
    tmp_path: Path, tool: str
) -> None:
    out = _stop(tmp_path, _copilot_transcript(tmp_path, 25, tool))
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


@pytest.mark.parametrize("read", ["mcp__hivemind__hive_search", "mcp__hivemind__hive_get"])
def test_a_session_that_wrote_but_never_rated_after_reading_is_blocked(
    tmp_path: Path, read: str
) -> None:
    t = _transcript(tmp_path, 30, read, "mcp__hivemind__hive_write")
    assert json.loads(_stop(tmp_path, t).stdout)["decision"] == "block"


def test_a_session_that_read_and_rated_is_not_blocked_even_if_it_wrote(tmp_path: Path) -> None:
    names = ("mcp__hivemind__hive_search", "mcp__hivemind__hive_write")
    for i, extra in enumerate(((), names)):
        t = _transcript(tmp_path, 30, *extra, "mcp__hivemind__hive_feedback")
        out = _stop(tmp_path, t, session_id=f"r{i}")
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


def _marker_dir(tmp_path: Path) -> Path:
    return tmp_path / "tmp" / f"hivemind-stop-reminder-{os.getuid()}"


def _markers(folder: Path) -> list[Path]:
    return [p for p in folder.iterdir() if p.is_file()]


def test_the_marker_folder_is_per_user_and_private(tmp_path: Path) -> None:
    assert json.loads(_stop(tmp_path, _transcript(tmp_path, 25)).stdout)["decision"] == "block"
    folder = _marker_dir(tmp_path)
    [marker] = _markers(folder)
    assert folder.stat().st_mode & 0o777 == 0o700
    assert marker.stat().st_mode & 0o077 == 0
    assert not (tmp_path / "tmp" / "hivemind-stop-reminder").exists()


def test_xdg_runtime_dir_holds_the_marker_when_it_is_ours(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    env = _env(tmp_path, XDG_RUNTIME_DIR=str(runtime))
    assert json.loads(_stop(tmp_path, _transcript(tmp_path, 25), env).stdout)["decision"] == "block"
    assert len(_markers(runtime / "hivemind-stop-reminder")) == 1
    assert (runtime / "hivemind-stop-reminder").stat().st_mode & 0o777 == 0o700
    assert not _marker_dir(tmp_path).exists()


def test_an_unusable_xdg_runtime_dir_falls_back_to_tmpdir(tmp_path: Path) -> None:
    env = _env(tmp_path, XDG_RUNTIME_DIR=str(tmp_path / "missing"))
    assert json.loads(_stop(tmp_path, _transcript(tmp_path, 25), env).stdout)["decision"] == "block"
    assert len(_markers(_marker_dir(tmp_path))) == 1


def test_a_group_or_other_writable_marker_folder_is_refused(tmp_path: Path) -> None:
    _env(tmp_path)
    folder = _marker_dir(tmp_path)
    folder.mkdir()
    folder.chmod(0o777)
    out = _stop(tmp_path, _transcript(tmp_path, 25))
    assert (out.returncode, out.stdout) == (0, "")
    assert list(folder.iterdir()) == []


def test_a_symlinked_marker_folder_is_refused(tmp_path: Path) -> None:
    _env(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    _marker_dir(tmp_path).symlink_to(elsewhere)
    out = _stop(tmp_path, _transcript(tmp_path, 25))
    assert (out.returncode, out.stdout) == (0, "")
    assert list(elsewhere.iterdir()) == []


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


@pytest.mark.parametrize("extra", [{}, {"HIVEMIND_INCOGNITO": "1"}])
def test_copilot_cli_gets_a_flat_additional_context(tmp_path: Path, extra: dict[str, str]) -> None:
    """Copilot CLI 1.0.95 reads only a top-level `additionalContext` and drops
    `hookSpecificOutput`; it sets COPILOT_CLI=1 for its hooks (checked live)."""
    out = subprocess.run(
        ["sh", str(START)],
        input='{"hook_event_name":"SessionStart","source":"new"}',
        env=_env(tmp_path, COPILOT_CLI="1", **extra),
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(out.stdout)
    assert list(payload) == ["additionalContext"]
    assert payload["additionalContext"].startswith("HIVEMIND:")
    assert ("incognito" in payload["additionalContext"]) == bool(extra)


def test_only_the_compact_source_gets_the_post_compaction_line(tmp_path: Path) -> None:
    assert COMPACT_LINE
    line = COMPACT_LINE.group(1)
    compact = _start(tmp_path, '{"hook_event_name":"SessionStart","source":"compact"}')
    assert line in compact and "hive_write" in compact and "hive_feedback" in compact
    for source in ("startup", "resume", "clear", "fork"):
        text = _start(tmp_path, json.dumps({"source": source}))
        assert line not in text and "compacted" not in text
    assert line not in _start(tmp_path, "")


def test_incognito_compact_stays_incognito(tmp_path: Path) -> None:
    assert COMPACT_LINE
    text = _start(tmp_path, '{"source":"compact"}', HIVEMIND_INCOGNITO="1")
    assert "incognito" in text and COMPACT_LINE.group(1) not in text and "compacted" not in text


# -- Pi and Oh My Pi: the same two nudges from the extension ----------------------

PI_EXTENSION = PLUGIN / "extensions" / "hivemind.ts"
STOP_TEXT = re.search(r"'(\{\"decision\":\"block\".*\})'", STOP.read_text())
COMPACT_LINE = re.search(r'extra="( Context was just compacted:[^"]*)"', START.read_text())

_PI_SCRIPT = """
const mod = await import(process.argv[1])
const h = {}
mod.default({ on: (name, fn) => { h[name] = fn } })
const out = {}
for (const step of JSON.parse(process.env.STEPS)) {
  const [kind, arg] = step
  if (kind === "call") h.tool_execution_start({ type: "tool_execution_start", ...arg })
  if (kind === "settle") (out.settle ??= []).push(h.agent_before_settle({ outcome: "completed", entries: [], ...arg }) ?? null)
  if (kind === "stop") (out.stop ??= []).push(h.session_stop({ stop_hook_active: false, ...arg }) ?? null)
  if (kind === "start") h.session_start({ reason: "new" })
  if (kind === "compact") h.session_compact({})
  if (kind === "prompt") (out.prompt ??= []).push(h.before_agent_start({ systemPrompt: ["base"] }).systemPrompt[1])
}
console.log(JSON.stringify(out))
"""


def _pi(steps: list[object], **env: str) -> dict[str, list[object]]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    out = subprocess.run(
        ["node", "--input-type=module", "-e", _PI_SCRIPT, PI_EXTENSION.as_uri()],
        env={**base, "HIVEMIND_API_KEY": "hm_key", "STEPS": json.dumps(steps), **env},
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, list[object]] = json.loads(out.stdout)
    return result


def _calls(n: int, name: str = "bash", **args: object) -> list[object]:
    return [["call", {"toolName": name, "args": args}] for _ in range(n)]


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def test_the_extension_shares_the_hook_texts() -> None:
    assert STOP_TEXT and COMPACT_LINE
    reason = json.loads(STOP_TEXT.group(1))["reason"]
    text = PI_EXTENSION.read_text()
    assert f'"{reason}"' in text
    assert f'"{COMPACT_LINE.group(1)}"' in text


@needs_node
def test_pi_continues_a_long_session_once_with_the_reminder() -> None:
    reason = json.loads(STOP_TEXT.group(1))["reason"]  # type: ignore[union-attr]
    r = _pi([*_calls(20), ["settle", {}], ["settle", {}]])
    first, second = r["settle"]
    assert first == {
        "entries": [
            {
                "type": "custom_message",
                "customType": "hivemind-reminder",
                "content": reason,
                "display": True,
            }
        ],
        "continue": True,
    }
    assert second is None


@needs_node
def test_oh_my_pi_blocks_a_long_session_once() -> None:
    r = _pi([*_calls(20), ["stop", {}], ["stop", {}]])
    first, second = r["stop"]
    assert first["decision"] == "block"  # type: ignore[index]
    assert second is None
    assert _pi([*_calls(20), ["stop", {"stop_hook_active": True}]])["stop"] == [None]


@needs_node
@pytest.mark.parametrize(
    "call",
    [
        {"toolName": "mcp__hivemind__hive_write", "args": {}},
        {"toolName": "hivemind_hive_feedback", "args": {}},
        {"toolName": "mcp", "args": {"tool": "hivemind_hive_write", "args": {}}},
        {"toolName": "mcpScript", "args": {"code": "await tools.hivemind_hive_write({})"}},
        {"toolName": "write", "args": {"path": "xd://mcp__hivemind_hive_feedback/"}},
        {"toolName": "hive_write", "args": {}, "parentToolCallId": "c1"},
    ],
)
def test_a_session_that_wrote_or_rated_is_not_reminded(call: dict[str, object]) -> None:
    r = _pi([*_calls(25), ["call", call], ["settle", {}], ["stop", {}]])
    assert r == {"settle": [None], "stop": [None]}


@needs_node
@pytest.mark.parametrize(
    ("read", "wrote"),
    [
        ({"toolName": "mcp__hivemind__hive_search", "args": {}}, "hive_write"),
        ({"toolName": "hivemind_hive_get", "args": {}}, "hive_write"),
        ({"toolName": "mcp", "args": {"tool": "hivemind_hive_get", "args": {}}}, "hive_write"),
    ],
)
def test_a_pi_session_that_wrote_but_read_and_never_rated_is_reminded(
    read: dict[str, object], wrote: str
) -> None:
    write = ["call", {"toolName": wrote, "args": {}}]
    r = _pi([*_calls(25), ["call", read], write, ["settle", {}]])
    assert r["settle"][0] is not None
    rate = ["call", {"toolName": "hivemind_hive_feedback", "args": {}}]
    assert _pi([*_calls(25), ["call", read], write, rate, ["settle", {}]]) == {"settle": [None]}


@needs_node
def test_reading_or_a_short_session_is_not_enough() -> None:
    r = _pi([*_calls(19), *_calls(1, "mcp__hivemind__hive_search"), ["settle", {}]])
    assert r["settle"][0] is not None  # a search is a call, not a contribution
    assert _pi([*_calls(19), ["settle", {}]]) == {"settle": [None]}
    # Calls a script made for the model are not counted as the model's.
    nested = [["call", {"toolName": "bash", "args": {}, "parentToolCallId": "c"}]] * 30
    assert _pi([*nested, ["settle", {}]]) == {"settle": [None]}


@needs_node
def test_the_pi_reminder_is_off_when_aborted_incognito_or_unconfigured() -> None:
    steps = [*_calls(25), ["settle", {}]]
    assert _pi([*_calls(25), ["settle", {"outcome": "aborted"}]]) == {"settle": [None]}
    assert _pi(steps, HIVEMIND_INCOGNITO="1") == {"settle": [None]}
    assert _pi(steps, HIVEMIND_API_KEY="") == {"settle": [None]}
    assert _pi([*_calls(3), ["settle", {}]], HIVEMIND_STOP_MIN_TOOLS="3")["settle"][0] is not None


@needs_node
def test_a_new_session_can_be_reminded_again() -> None:
    r = _pi([*_calls(20), ["settle", {}], ["start", None], *_calls(20), ["settle", {}]])
    assert all(s is not None for s in r["settle"])


@needs_node
def test_the_section_after_a_compaction_asks_once_to_write() -> None:
    line = COMPACT_LINE.group(1)  # type: ignore[union-attr]
    r = _pi([["prompt", None], ["compact", None], ["prompt", None], ["prompt", None]])
    before, after, later = r["prompt"]
    assert isinstance(after, str) and after.endswith(line)
    assert line not in str(before) and line not in str(later)
    incognito = _pi([["compact", None], ["prompt", None]], HIVEMIND_INCOGNITO="1")
    assert line not in str(incognito["prompt"][0])


# -- OpenCode: the same two nudges from the plugin ---------------------------------

OPENCODE_PLUGIN = PLUGIN / "opencode" / "hivemind.js"

_OPENCODE_SCRIPT = """
const mod = await import(process.argv[1])
const sent = []
const parents = JSON.parse(process.env.PARENTS ?? "{}")
const client = { session: {
  get: async ({ path }) => ({ data: { id: path.id, parentID: parents[path.id] } }),
  promptAsync: async (req) => { sent.push(req) },
} }
const hooks = await mod.default.server({ client })
const out = { sent, system: [] }
for (const [kind, arg] of JSON.parse(process.env.STEPS)) {
  if (kind === "message") await hooks["chat.message"](arg, { message: {}, parts: [] })
  if (kind === "call") await hooks["tool.execute.after"](arg, { title: "", output: "", metadata: {} })
  if (kind === "event") await hooks.event({ event: arg })
  if (kind === "system") {
    const o = { system: [] }
    await hooks["experimental.chat.system.transform"](arg, o)
    out.system.push(o.system[0])
  }
}
console.log(JSON.stringify(out))
"""


def _opencode(steps: list[object], **env: str) -> dict[str, list[object]]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    out = subprocess.run(
        ["node", "--input-type=module", "-e", _OPENCODE_SCRIPT, OPENCODE_PLUGIN.as_uri()],
        env={**base, "HIVEMIND_API_KEY": "hm_key", "STEPS": json.dumps(steps), **env},
        capture_output=True,
        text=True,
        check=True,
    )
    result: dict[str, list[object]] = json.loads(out.stdout)
    return result


def _oc_calls(n: int, tool: str = "bash", sid: str = "s1") -> list[object]:
    return [["call", {"tool": tool, "sessionID": sid, "callID": f"c{i}"}] for i in range(n)]


def _idle(sid: str = "s1") -> list[object]:
    return ["event", {"type": "session.idle", "properties": {"sessionID": sid}}]


def test_the_opencode_plugin_shares_the_hook_texts() -> None:
    assert STOP_TEXT and COMPACT_LINE
    text = OPENCODE_PLUGIN.read_text()
    assert f'"{json.loads(STOP_TEXT.group(1))["reason"]}"' in text
    assert f'"{COMPACT_LINE.group(1)}"' in text


@needs_node
def test_opencode_prompts_a_long_idle_session_once_with_its_own_model() -> None:
    reason = json.loads(STOP_TEXT.group(1))["reason"]  # type: ignore[union-attr]
    model = {"providerID": "p", "modelID": "m"}
    r = _opencode(
        [
            ["message", {"sessionID": "s1", "agent": "build", "model": model}],
            *_oc_calls(20),
            _idle(),
            _idle(),
        ]
    )
    [sent] = r["sent"]
    assert sent == {
        "path": {"id": "s1"},
        "body": {"agent": "build", "model": model, "parts": [{"type": "text", "text": reason}]},
    }


@needs_node
@pytest.mark.parametrize("tool", ["hivemind_hive_write", "hivemind_hive_feedback"])
def test_an_opencode_session_that_wrote_or_rated_is_not_prompted(tool: str) -> None:
    assert _opencode([*_oc_calls(25), *_oc_calls(1, tool), _idle()])["sent"] == []


@needs_node
def test_an_opencode_session_that_wrote_but_read_and_never_rated_is_prompted() -> None:
    wrote = _oc_calls(1, "hivemind_hive_write")
    for read in ("hivemind_hive_search", "hivemind_hive_get"):
        r = _opencode([*_oc_calls(25), *_oc_calls(1, read), *wrote, _idle()])
        assert len(r["sent"]) == 1
    rated = _oc_calls(1, "hivemind_hive_feedback")
    assert _opencode([*_oc_calls(25, "hivemind_hive_get"), *wrote, *rated, _idle()])["sent"] == []


@needs_node
def test_opencode_leaves_short_subagent_incognito_and_unconfigured_sessions_alone() -> None:
    assert _opencode([*_oc_calls(19, "hivemind_hive_search"), _idle()])["sent"] == []
    long = [*_oc_calls(25, sid="child"), _idle("child")]
    assert _opencode(long, PARENTS=json.dumps({"child": "s1"}))["sent"] == []
    assert _opencode([*_oc_calls(25), _idle()], HIVEMIND_INCOGNITO="1")["sent"] == []
    assert _opencode([*_oc_calls(25), _idle()], HIVEMIND_API_KEY="")["sent"] == []
    # Sessions are counted apart.
    split = [*_oc_calls(10, sid="a"), *_oc_calls(10, sid="b"), _idle("a"), _idle("b")]
    assert _opencode(split)["sent"] == []


@needs_node
def test_the_opencode_reminder_after_a_compaction_asks_once_to_write() -> None:
    line = COMPACT_LINE.group(1)  # type: ignore[union-attr]
    compacted = ["event", {"type": "session.compacted", "properties": {"sessionID": "s1"}}]
    system = ["system", {"sessionID": "s1", "model": {}}]
    other = ["system", {"sessionID": "s2", "model": {}}]
    r = _opencode([system, compacted, other, system, system])
    before, elsewhere, after, later = r["system"]
    assert isinstance(after, str) and after.endswith(line)
    assert all(line not in str(s) for s in (before, elsewhere, later))


@needs_node
def test_the_opencode_v1_hook_honours_kilocodes_v2_event_names() -> None:
    """KiloCode (an OpenCode fork) delivers v2 events to the v1 event hook:
    an execution end fires the stop nudge, compaction.ended carries the
    post-compaction line, and a shutdown interruption stays excluded."""
    succeeded = [
        "event",
        {"type": "session.execution.succeeded", "properties": {"sessionID": "s1"}},
    ]
    assert [s["path"]["id"] for s in _opencode([*_oc_calls(20), succeeded])["sent"]] == ["s1"]
    shutdown = [
        "event",
        {
            "type": "session.execution.interrupted",
            "properties": {"sessionID": "s2", "reason": "shutdown"},
        },
    ]
    assert _opencode([*_oc_calls(20, sid="s2"), shutdown])["sent"] == []
    interrupted = [
        "event",
        {"type": "session.execution.interrupted", "properties": {"sessionID": "s3"}},
    ]
    assert [
        s["path"]["id"] for s in _opencode([*_oc_calls(20, sid="s3"), interrupted])["sent"]
    ] == ["s3"]
    line = COMPACT_LINE.group(1)  # type: ignore[union-attr]
    compacted = ["event", {"type": "session.compaction.ended", "properties": {"sessionID": "s1"}}]
    system = ["system", {"sessionID": "s1", "model": {}}]
    r = _opencode([system, compacted, system, system])
    assert isinstance(r["system"][1], str) and r["system"][1].endswith(line)
    assert all(line not in str(s) for s in (*r["system"][:1], *r["system"][2:]))


# -- DeepSeek Harness: the Stop reminder from the bundle's own plugin ---------------

DSH_PLUGIN = PLUGIN / "dsh" / "hivemind-reminders.js"

_DSH_SCRIPT = """
const mod = await import(process.argv[1])
const handlers = {}
const visible = new Set(JSON.parse(process.env.VISIBLE ?? '["mcp__hivemind__hive_write"]'))
const ctx = {
  on: (name, fn) => { handlers[name] = fn },
  tools: { get: (name, agent) => (visible.has(name) && agent ? { name } : undefined) },
}
mod.apply(ctx)
const steered = []
const agents = {}
const agent = (id) => (agents[id] ??= { id, steer: (m) => steered.push({ id, m }) })
for (const [kind, arg] of JSON.parse(process.env.STEPS)) {
  if (kind === "subagent") handlers["subagent/start"]({ id: arg })
  if (kind === "call") {
    const exec = { name: arg.name, agent: agent(arg.agent ?? "main"), arguments: {} }
    const out = await handlers["tools/post-execute"](exec, {}, async () => "downstream")
    if (out !== "downstream") throw new Error("post-execute must pass through")
  }
  if (kind === "stop") await handlers["agent/turn-stopping"]({ agent: agent(arg ?? "main"), turn: 1 })
}
console.log(JSON.stringify(steered))
"""


def _dsh(steps: list[object], **env: str) -> list[dict[str, object]]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    out = subprocess.run(
        ["node", "--input-type=module", "-e", _DSH_SCRIPT, DSH_PLUGIN.as_uri()],
        env={**base, "STEPS": json.dumps(steps), **env},
        capture_output=True,
        text=True,
        check=True,
    )
    result: list[dict[str, object]] = json.loads(out.stdout)
    return result


def _dsh_calls(n: int, name: str = "bash", agent: str = "main") -> list[object]:
    return [["call", {"name": name, "agent": agent}] for _ in range(n)]


def test_the_dsh_bundle_loads_the_reminder_plugin_with_the_hook_text() -> None:
    assert STOP_TEXT
    assert f"'{json.loads(STOP_TEXT.group(1))['reason']}'" in DSH_PLUGIN.read_text()
    patch = (PLUGIN / "cordis.patch.yml").read_text()
    assert "name: ./dsh/hivemind-reminders.js" in patch


@needs_node
def test_dsh_steers_a_long_turn_once_with_a_user_message() -> None:
    reason = json.loads(STOP_TEXT.group(1))["reason"]  # type: ignore[union-attr]
    [steer] = _dsh([*_dsh_calls(20), ["stop", None], *_dsh_calls(3), ["stop", None]])
    m = steer["m"]
    assert isinstance(m, dict)
    assert (m["role"], m["content"], m["source"]) == (
        "user",
        [{"type": "text", "text": reason}],
        {"kind": "hivemind"},
    )
    assert m["id"]


@needs_node
@pytest.mark.parametrize("tool", ["mcp__hivemind__hive_write", "mcp__hivemind__hive_feedback"])
def test_a_dsh_session_that_wrote_or_rated_is_not_steered(tool: str) -> None:
    assert _dsh([*_dsh_calls(25), *_dsh_calls(1, tool), ["stop", None]]) == []


@needs_node
def test_a_dsh_session_that_wrote_but_read_and_never_rated_is_steered() -> None:
    wrote = _dsh_calls(1, "mcp__hivemind__hive_write")
    for read in ("mcp__hivemind__hive_search", "mcp__hivemind__hive_get"):
        assert len(_dsh([*_dsh_calls(25), *_dsh_calls(1, read), *wrote, ["stop", None]])) == 1
    rated = _dsh_calls(1, "mcp__hivemind__hive_feedback")
    assert _dsh([*_dsh_calls(25, "mcp__hivemind__hive_get"), *wrote, *rated, ["stop", None]]) == []


@needs_node
def test_dsh_leaves_short_subagent_and_disconnected_sessions_alone() -> None:
    assert _dsh([*_dsh_calls(19, "mcp__hivemind__hive_search"), ["stop", None]]) == []
    child = [["subagent", "kid"], *_dsh_calls(25, agent="kid"), ["stop", "kid"], ["stop", None]]
    assert _dsh(child) == []
    # Incognito or no URL: the server row is off, so hive_write is not visible.
    assert _dsh([*_dsh_calls(25), ["stop", None]], VISIBLE="[]") == []
    assert _dsh([*_dsh_calls(4), ["stop", None]], HIVEMIND_STOP_MIN_TOOLS="3") != []
