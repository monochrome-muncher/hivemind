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
const hooks = await mod.HivemindPlugin({ client })
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
