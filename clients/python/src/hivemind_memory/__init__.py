"""Async client for Hivemind, the shared memory service for AI agents."""

from hivemind_memory._version import __version__
from hivemind_memory.client import Hivemind
from hivemind_memory.conversation import Conversation
from hivemind_memory.errors import AuthenticationError, HivemindError
from hivemind_memory.replies import EntryKind, Recency, Verdict
from hivemind_memory.text import COMPACTED, INSTRUCTIONS, REMINDER

__all__ = [
    "COMPACTED",
    "INSTRUCTIONS",
    "REMINDER",
    "AuthenticationError",
    "Conversation",
    "EntryKind",
    "Hivemind",
    "HivemindError",
    "Recency",
    "Verdict",
    "__version__",
]
