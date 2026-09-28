"""Hivemind — Hermes plugin registration.

Two layers, both static and offline (no network call, so neither can fail
or slow a session down):

1. The two skills in ``skills/`` (hivemind, hivemind-setup), registered
   under this plugin's namespace: ``skill_view("hivemind:hivemind")``.
   Plugin skills are read-only and opt-in explicit loads, so the
   system-prompt section below is what points the agent at them.
2. A bounded system-prompt section that keeps the agent Hivemind-aware
   across sessions and compaction: it renders once for a new session, is
   frozen on compression, and is recovered from the persisted system
   prompt after a process restart/resume. That is the same shape as the
   DeepSeek Harness MCP instructions and the Claude Code/Codex
   SessionStart hook — the reminder lives in the system prompt, which
   compaction never rewrites.

The Hivemind MCP server is configured separately (the
``mcp_servers.hivemind`` entry in ``~/.hermes/config.yaml``, walked
through by the hivemind-setup skill); the key never enters this install
tree. The section reads ``HIVEMIND_API_KEY`` at session start and reports
the connection state without ever printing the key.
"""

from __future__ import annotations

import os
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
SKILLS_DIR = PLUGIN_DIR / "skills"

# The common core is shared, byte for byte, with the Claude Code/Codex
# SessionStart hook (skills/hivemind-setup/scripts/hivemind-session-start.sh)
# and the Pi extension (extensions/hivemind.ts). Keep the three in step —
# tests/unit/test_agent_plugin.py checks it.
_CORE = (
    "HIVEMIND: your organization's Hivemind is your long-term memory. "
    "Follow the hivemind skill (load it now if it is not in context). "
    "Recall with hive_search before non-trivial work; contribute what you "
    "learn with hive_write, as often as you have something worth "
    "reusing; prefer Hivemind over local memory files."
)

_CONFIGURED = (
    "Hivemind is configured. Your first action this session: call "
    "hive_whoami and act on the result as the hivemind skill describes."
)

_NOT_CONFIGURED = (
    "HIVEMIND_API_KEY is not set in this environment, so Hivemind is "
    "probably not connected. If the hive_* tools are missing or fail, "
    "tell the user once and offer to run the hivemind-setup skill."
)

# An incognito session (ADR 0035): the whole section is replaced by this
# sentence, shared byte for byte with the hook and the Pi extension.
_INCOGNITO = (
    "HIVEMIND: this is an incognito session, so Hivemind is completely of"
    "f. Do not call any hive_* tool, and do not mention Hivemind or offer"
    " to set it up. You may keep local notes; start each one with [hivemi"
    "nd: incognito, never upload] so that no later session uploads it. If"
    " hive_* tools are loaded anyway, still do not use them, and tell the"
    " user once that the tools are loaded, so this session is incognito o"
    "nly by your own restraint."
)

_TRUTHY = {"1", "true", "yes", "on"}


def _incognito() -> bool:
    return os.environ.get("HIVEMIND_INCOGNITO", "").strip().lower() in _TRUTHY


# Hermes plugin skills are namespaced and not listed in the system
# prompt's skill index; say exactly how to load them.
_NAMESPACED_LOAD = 'In this harness the skill is namespaced: skill_view("hivemind:hivemind").'


def _hivemind_section() -> str:
    if _incognito():
        return _INCOGNITO
    state = _CONFIGURED if os.environ.get("HIVEMIND_API_KEY") else _NOT_CONFIGURED
    return f"{_CORE} {_NAMESPACED_LOAD} {state}"


def register(ctx) -> None:
    """Wire the skills and the stay-aware system-prompt section."""
    for child in sorted(SKILLS_DIR.iterdir()):
        skill_md = child / "SKILL.md"
        if child.is_dir() and skill_md.is_file():
            ctx.register_skill(child.name, skill_md)

    # register_system_prompt_section is the documented stay-aware
    # mechanism; probe for it so an older Hermes that lacks it degrades
    # to the skills alone (Hermes's own guidance: probe before using,
    # degrade gracefully).
    register_section = getattr(ctx, "register_system_prompt_section", None)
    if register_section is not None:
        register_section(
            "hivemind",
            _hivemind_section,
            position="after_memory",
            max_chars=4000,
        )
