#!/bin/sh
# Hivemind SessionStart hook: prints a short reminder that the harness adds
# to the agent's context on startup, resume, /clear and after compaction.
#
# Used by the hivemind plugin (hooks/hooks.json) and by hand-installed hooks
# (see the hivemind-setup skill). It is deliberately static and offline: no
# network call, so it cannot fail or slow a session down. It only checks
# whether HIVEMIND_API_KEY is set (the URL may live in the harness config,
# as it does for Codex). It never prints the key.

if [ -z "${HIVEMIND_API_KEY:-}" ]; then
  state="HIVEMIND_API_KEY is not set in this environment, so Hivemind is probably not connected. If the hive_* tools are missing or fail, tell the user once and offer to run the hivemind-setup skill."
else
  state="Hivemind is configured. Your first action this session: call hive_whoami and act on the result as the hivemind skill describes."
fi

text="HIVEMIND: your organization's Hivemind is your long-term memory. Follow the hivemind skill (load it now if it is not in context). Recall with hive_search before non-trivial work; contribute what you learn with hive_write, as often as you have something worth reusing; prefer Hivemind over local memory files. ${state}"

# One JSON object on stdout: understood by Claude Code and Codex. The text
# contains no double quotes or backslashes, so it needs no escaping.
printf '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"%s"}}\n' "$text"
