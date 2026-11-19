"""Async client for Hivemind, the shared memory service for AI agents."""

from hivemind_memory._version import __version__
from hivemind_memory.client import Hivemind
from hivemind_memory.errors import AuthenticationError, HivemindError
from hivemind_memory.text import REMINDER

__all__ = ["REMINDER", "AuthenticationError", "Hivemind", "HivemindError", "__version__"]
