"""When to send the end-of-task reminder: the plugin's Stop hook rule, for one conversation."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hivemind_memory import text

if TYPE_CHECKING:
    from hivemind_memory.client import Hivemind

MIN_TOOL_CALLS_ENV = "HIVEMIND_STOP_MIN_TOOLS"
DEFAULT_MIN_TOOL_CALLS = 20

# A Hivemind tool under any prefix a framework gives it (mcp__hivemind__hive_write, ...).
_WROTE = re.compile(r"(^|[^a-z])hive_write$")
_RATED = re.compile(r"(^|[^a-z])hive_feedback$")
_READ = re.compile(r"(^|[^a-z])hive_(search|get)$")


def _min_tool_calls() -> int:
    value = os.environ.get(MIN_TOOL_CALLS_ENV, "")
    return int(value) if re.fullmatch(r"[0-9]+", value) else DEFAULT_MIN_TOOL_CALLS


class Conversation:
    """Counts one conversation's tool calls and says when the end-of-task reminder is due.

    The rule is the plugin's Stop hook: once per conversation, after at least
    ``min_tool_calls`` tool calls (``HIVEMIND_STOP_MIN_TOOLS``, default 20), when nothing was
    rated yet and either nothing was written or entries were read. Start one per conversation
    with :meth:`Hivemind.conversation`.
    """

    def __init__(self, hive: Hivemind, min_tool_calls: int | None = None) -> None:
        self.hive = hive
        self.min_tool_calls = _min_tool_calls() if min_tool_calls is None else min_tool_calls
        self.tool_calls = 0
        self._counted: set[str] = set()
        self._wrote = self._rated = self._read = self._reminded = False

    async def run_tool(self, name: str, arguments: Mapping[str, Any] | str) -> str:
        """:meth:`Hivemind.run_tool`, counted."""
        self.count_tool_call(name)
        return await self.hive.run_tool(name, arguments)

    def count_tool_call(self, name: str, call_id: str | None = None) -> None:
        """Count a tool call this conversation made outside :meth:`run_tool`, such as the app's own tools.

        A call counted before under the same ``call_id`` is not counted again.
        """
        if call_id is not None:
            if call_id in self._counted:
                return
            self._counted.add(call_id)
        self.tool_calls += 1
        self._wrote |= bool(_WROTE.search(name))
        self._rated |= bool(_RATED.search(name))
        self._read |= bool(_READ.search(name))

    def reminder_due(self) -> str | None:
        """The reminder when it is due, once; otherwise ``None``. Send it as one more user turn."""
        quiet = self._rated or (self._wrote and not self._read)
        if self._reminded or quiet or self.tool_calls < self.min_tool_calls:
            return None
        self._reminded = True
        return text.REMINDER
