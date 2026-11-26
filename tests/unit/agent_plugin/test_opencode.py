"""OpenCode, Oh My Pi and the shared extension (1.2.0)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.unit.agent_plugin.common import (
    _OPENCODE_SCRIPT,
    COMPACTED_TEXT,
    INCOGNITO_TEXT,
    OPENCODE_PLUGIN,
    PLUGIN,
    SKILL_NAMES,
    STAY_AWARE_CORE,
    STOP_REMINDER_TEXT,
    _node,
    needs_node,
)


@needs_node
def test_opencode_plugin_registers_the_server_and_skills() -> None:
    r = _node(
        _OPENCODE_SCRIPT,
        OPENCODE_PLUGIN,
        HIVEMIND_MCP_URL="https://hm.example/mcp",
        HIVEMIND_API_KEY="hm_secret_value",
    )
    assert r["id"] == "hivemind"
    # The dual shape: a plain-object default export carrying both the v2
    # setup and the v1 server entry points.
    assert r["hasSetup"] is True and r["hasServer"] is True
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


_OPENCODE_ONE_SYSTEM_MESSAGE_SCRIPT = """
const def = (await import(process.argv[1])).default
const v1 = await def.server({})
const out = { system: ["base prompt"] }
await v1["experimental.chat.system.transform"]({ model: {} }, out)
const hooks = {}
await def.setup({ session: { hook: async (name, cb) => { hooks[name] = cb } } })
const event = { sessionID: "s1", system: [{ type: "text", text: "base prompt" }] }
await hooks["context"](event)
console.log(JSON.stringify({ v1: out.system, v2: event.system }))
"""


@needs_node
def test_opencode_plugin_adds_no_second_system_message() -> None:
    """Many chat templates refuse a second system message with a 400, and
    OpenCode sends each entry of the system list as its own message."""
    r = _node(_OPENCODE_ONE_SYSTEM_MESSAGE_SCRIPT, OPENCODE_PLUGIN, HIVEMIND_API_KEY="k")
    [v1] = r["v1"]
    [v2] = r["v2"]
    assert v1.startswith("base prompt\n\n" + STAY_AWARE_CORE)
    assert v2["type"] == "text" and v2["text"] == v1


# The v2 half of the dual export: setup(ctx) registers the same behaviour on
# v2's domains (mcp/skill transforms, session+tool hooks, the event stream).
# The mock ctx records every registration and the prompts it receives; the
# events drive the compaction line and the once-per-session stop reminder.
_OPENCODE_V2_SCRIPT = """
const mod = await import(process.argv[1])
const def = mod.default

const mcp = new Map(Object.entries(JSON.parse(process.env.MANUAL_MCP ?? "{}")))
const mcpEditor = {
  get: (name) => mcp.get(name),
  set: (name, cfg) => mcp.set(name, cfg),
  update: (name, fn) => { if (mcp.has(name)) fn(mcp.get(name)) },
  remove: (name) => mcp.delete(name),
  list: () => [...mcp],
}
const skills = new Map()
const skillEditor = {
  get: (name) => skills.get(name),
  add: (s) => skills.set(s.id, s),
  update: (name, fn) => { if (skills.has(name)) fn(skills.get(name)) },
  remove: (name) => skills.delete(name),
  list: () => [...skills.values()],
}
const sessions = new Map(Object.entries(JSON.parse(process.env.SESSIONS ?? "{}")))
const hooks = {}
const prompts = []
const ctx = {
  mcp: { transform: async (cb) => cb(mcpEditor) },
  skill: { transform: (cb) => cb(skillEditor) },
  session: {
    hook: async (name, cb) => { hooks[name] = cb; return { dispose() {} } },
    get: async ({ sessionID }) => sessions.get(sessionID) ?? { sessionID },
    prompt: async (input) => { prompts.push(input) },
  },
  tool: { hook: async (name, cb) => { hooks[name] = cb; return { dispose() {} } } },
  event: {
    subscribe: () => (async function* () {
      for (const e of JSON.parse(process.env.EVENTS ?? "[]")) yield e
    })(),
  },
}

const cleanup = await def.setup(ctx)
for (const t of JSON.parse(process.env.TOOLS ?? "[]")) await hooks["execute.after"](t)
await new Promise((r) => setTimeout(r, 50))  // let the event stream drain first
const first = { sessionID: "s1", system: [] }
await hooks["context"](first)
const second = { sessionID: "s1", system: [] }
await hooks["context"](second)

console.log(JSON.stringify({
  id: def.id,
  hasServer: typeof def.server === "function",
  cleanup: typeof cleanup === "function",
  mcp: [...mcp],
  skills: [...skills.values()].map(({ id, name, description, path, content }) => ({ id, name, description, path, contentLen: content.length })),
  prompts,
  system: [first.system, second.system],
}))
"""


_V2_SESSIONS = json.dumps(
    {
        "s1": {"sessionID": "s1"},
        "child": {"sessionID": "child", "parentID": "s1"},
        "s2": {"sessionID": "s2"},
    }
)


# s1: three plain calls -> reminded; child: three calls but a subagent;
# s2: three calls including a contribution -> not reminded.
_V2_TOOLS = json.dumps(
    [
        {"tool": "read", "sessionID": "s1"},
        {"tool": "bash", "sessionID": "s1"},
        {"tool": "write", "sessionID": "s1"},
        {"tool": "read", "sessionID": "child"},
        {"tool": "bash", "sessionID": "child"},
        {"tool": "read", "sessionID": "child"},
        {"tool": "read", "sessionID": "s2"},
        {"tool": "hivemind_hive_write", "sessionID": "s2"},
        {"tool": "read", "sessionID": "s2"},
    ]
)


# v2 terminal events: an execution ends with succeeded/failed/interrupted
# (v1's session.idle fired on each of them); compaction ends with
# session.compaction.ended. The shutdown interruption stays excluded.
_V2_EVENTS = json.dumps(
    [
        {"type": "session.compaction.ended", "data": {"sessionID": "s1"}},
        {"type": "session.execution.succeeded", "data": {"sessionID": "s1"}},
        {"type": "session.execution.succeeded", "data": {"sessionID": "child"}},
        {"type": "session.execution.succeeded", "data": {"sessionID": "s2"}},
        {
            "type": "session.execution.interrupted",
            "data": {"sessionID": "s1", "reason": "shutdown"},
        },
        {"type": "session.execution.succeeded", "data": {"sessionID": "s1"}},
    ]
)


@needs_node
def test_opencode_v2_setup_registers_server_skills_and_hooks() -> None:
    r = _node(
        _OPENCODE_V2_SCRIPT,
        OPENCODE_PLUGIN,
        HIVEMIND_MCP_URL="https://hm.example/mcp",
        HIVEMIND_API_KEY="hm_secret_value",
        HIVEMIND_STOP_MIN_TOOLS="3",
        SESSIONS=_V2_SESSIONS,
        TOOLS=_V2_TOOLS,
        EVENTS=_V2_EVENTS,
    )
    assert r["id"] == "hivemind" and r["hasServer"] is True and r["cleanup"] is True
    # MCP: registered from the environment; v2 uses `disabled`, not `enabled`.
    [pair] = r["mcp"]
    name, server = pair
    assert name == "hivemind"
    assert (server["type"], server["url"], server["oauth"], server["disabled"]) == (
        "remote",
        "https://hm.example/mcp",
        False,
        False,
    )
    assert server["headers"] == {"Authorization": "Bearer hm_secret_value"}
    # Skills: every one, by id, with the frontmatter name and description.
    by_id = {s["id"]: s for s in r["skills"]}
    assert sorted(by_id) == SKILL_NAMES
    for name in SKILL_NAMES:
        skill = by_id[name]
        assert skill["name"] == name
        assert skill["description"] and skill["contentLen"] > 0
        assert Path(skill["path"]).resolve() == (PLUGIN / "skills" / name / "SKILL.md").resolve()
    # The reminder is a {type, text} part; the compaction line rides the
    # request right after session.compaction.ended, and no later one.
    [first] = r["system"][0]
    assert first["type"] == "text"
    assert first["text"].startswith(STAY_AWARE_CORE) and "Hivemind is configured" in first["text"]
    assert first["text"].endswith(COMPACTED_TEXT)
    [second] = r["system"][1]
    assert "Context was just compacted" not in second["text"]
    # The stop reminder went out once, for s1 only (child is a subagent, s2
    # contributed), as a v2 prompt with the session's own agent and model.
    assert r["prompts"] == [
        {
            "sessionID": "s1",
            "text": STOP_REMINDER_TEXT,
        }
    ]


@needs_node
def test_opencode_v2_setup_goes_incognito() -> None:
    fresh = _node(
        _OPENCODE_V2_SCRIPT,
        OPENCODE_PLUGIN,
        HIVEMIND_INCOGNITO="1",
        HIVEMIND_MCP_URL="https://x/mcp",
        SESSIONS=_V2_SESSIONS,
        TOOLS=_V2_TOOLS,
        EVENTS=_V2_EVENTS,
    )
    assert fresh["mcp"] == []
    [reminder] = fresh["system"][0]
    assert reminder["text"] == INCOGNITO_TEXT
    assert fresh["prompts"] == []
    manual = _node(
        _OPENCODE_V2_SCRIPT,
        OPENCODE_PLUGIN,
        HIVEMIND_INCOGNITO="true",
        MANUAL_MCP=json.dumps({"hivemind": {"type": "remote", "url": "https://mine/mcp"}}),
    )
    [pair] = manual["mcp"]
    name, server = pair
    assert name == "hivemind" and server["disabled"] is True


# An existing "hivemind" server that points elsewhere still wins, but the
# plugin warns once per process on stderr, with hosts only and never the key.
_OPENCODE_SHADOW_SCRIPT = """
const mod = await import(process.argv[1])
const def = mod.default
const url = process.env.EXISTING_URL
if (process.env.PATH_KIND === "v1") {
  for (let i = 0; i < 2; i++) {
    const hooks = await def.server({})
    await hooks.config({ mcp: { hivemind: { type: "remote", url, enabled: true } } })
  }
} else {
  for (let i = 0; i < 2; i++) {
    const mcp = new Map([["hivemind", { type: "remote", url }]])
    const editor = { get: (n) => mcp.get(n), set: (n, c) => mcp.set(n, c), update: () => {} }
    await def.setup({ mcp: { transform: async (cb) => cb(editor) } })
  }
}
console.log("{}")
"""


def _shadow_warnings(path_kind: str, existing: str, **env: str) -> str:
    environ = {k: v for k, v in os.environ.items() if not k.startswith("HIVEMIND_")}
    environ.update(env, PATH_KIND=path_kind, EXISTING_URL=existing)
    out = subprocess.run(
        ["node", "--input-type=module", "-e", _OPENCODE_SHADOW_SCRIPT, OPENCODE_PLUGIN.as_uri()],
        env=environ,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stderr


@needs_node
@pytest.mark.parametrize("path_kind", ["v1", "v2"])
def test_opencode_plugin_warns_once_when_an_existing_server_points_elsewhere(
    path_kind: str,
) -> None:
    err = _shadow_warnings(
        path_kind,
        "https://other.example:8443/secret-path/mcp?token=abc",
        HIVEMIND_MCP_URL="https://hm.example/mcp",
        HIVEMIND_API_KEY="hm_secret_value",
    )
    assert err.count("hivemind:") == 1, err  # two loads in one process, one warning
    origins = re.findall(r"points at (\S+?)[,.]\s", err)
    assert origins == ["https://hm.example", "https://other.example:8443"], err
    assert "existing entry wins" in err and "hivemind-incognito" in err
    for leaked in ("hm_secret_value", "secret-path", "token=abc", "/mcp"):
        assert leaked not in err, err


@needs_node
@pytest.mark.parametrize("path_kind", ["v1", "v2"])
@pytest.mark.parametrize(
    ("existing", "env"),
    [
        ("https://hm.example/mcp/", {"HIVEMIND_MCP_URL": " https://hm.example/mcp "}),
        ("https://other.example/mcp", {}),
        ("https://other.example/mcp", {"HIVEMIND_MCP_URL": "", "HIVEMIND_API_KEY": "k"}),
        (
            "https://other.example/mcp",
            {"HIVEMIND_MCP_URL": "https://hm.example/mcp", "HIVEMIND_INCOGNITO": "1"},
        ),
    ],
    ids=["equal-after-normalising", "url-unset", "url-empty", "incognito"],
)
def test_opencode_plugin_is_silent_when_nothing_is_shadowed(
    path_kind: str, existing: str, env: dict[str, str]
) -> None:
    assert _shadow_warnings(path_kind, existing, **env) == ""
