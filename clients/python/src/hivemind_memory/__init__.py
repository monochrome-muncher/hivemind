"""Async client for Hivemind, the shared memory service for AI agents."""

from hivemind_memory._version import __version__
from hivemind_memory.client import Hivemind
from hivemind_memory.errors import AuthenticationError, HivemindError
from hivemind_memory.replies import EntryKind, Recency, Verdict
from hivemind_memory.text import INSTRUCTIONS, REMINDER

__all__ = [
    "INSTRUCTIONS",
    "REMINDER",
    "AuthenticationError",
    "EntryKind",
    "Hivemind",
    "HivemindError",
    "Recency",
    "Verdict",
    "__version__",
]
