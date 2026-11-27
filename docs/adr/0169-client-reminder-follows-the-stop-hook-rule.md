# The packages say when the end-of-task reminder is due, by the Stop hook's rule

Builds on [ADR 0162](0162-python-and-npm-client-packages.md) (`reminder()` is the Stop hook's text) and on the plugin's Stop hook and post-compaction line (`plugins/hivemind/DEVELOPING.md`, "The Stop hook and the post-compaction line").

Status: Accepted. Asked for on 2026-11-27.

## Context

The packages shipped the reminder's text and left the "when" to the app. The README loops sent it after every task, however short. The plugin's Stop hook sends it only once per session, after at least `HIVEMIND_STOP_MIN_TOOLS` (default 20) tool calls, when nothing was rated and either nothing was written or entries were read. Without that rule an app pays one more model turn for each short question, and asks an agent that already rated to rate again. The packages also had no counterpart of the line the plugin adds after a compaction.

## Decision

1. `hive.conversation()` starts a `Conversation`: it counts the calls run through its `run_tool()` / `runTool()` and those the app reports with `count_tool_call()` / `countToolCall()`, and notes a write, a rating or a read under any tool name prefix, as the Pi extension does. `reminder_due()` / `reminderDue()` returns the reminder once when the Stop hook's rule holds, otherwise nothing.
2. The threshold is the `min_tool_calls` / `minToolCalls` argument, else `HIVEMIND_STOP_MIN_TOOLS`, else 20, as in the hook.
3. `hive.after_compaction()` / `afterCompaction()` and the `COMPACTED` constant are the SessionStart hook's post-compaction sentence, copied by `make sync-client-text`.
4. A call counted with an id is counted once. `ainvoke_with_reminder` (Python, any agent whose state holds `messages`) and `generateTextWithReminder` (AI SDK) run the task, count every tool call in the messages or steps by its id, Hivemind's included, and if the reminder is due run one more turn with it. Counting from the transcript, as the hook does, also counts a call whose arguments failed validation and never ran, and a checkpointer that returns the whole thread does not count earlier turns twice. Neither needs a new dependency.
5. `tests/unit/test_client_packages.py` checks the copied sentence and that both packages read the hook's variable with its default.

## Considered and not done

- **A LangChain agent middleware.** It is the framework's own hook, but it lives in `langchain`, not `langchain-core`, so the `[langchain]` extra would grow. `ainvoke_with_reminder` gives the same behaviour on `langchain-core` alone.
- **Counting inside `Hivemind` itself.** One client serves many conversations; the count belongs to one.

## Consequences

- The README loops remind only when the plugin would.
- A change to the hook's rule needs the same change in both `conversation` modules; the test catches a changed variable or default, not a changed rule.
