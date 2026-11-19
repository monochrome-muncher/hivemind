"""The errors the client raises."""

from __future__ import annotations


class HivemindError(Exception):
    """A tool refused a call. ``code`` is the server's error code, for example ``not_found``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class AuthenticationError(HivemindError):
    """The key is rejected, unknown or locked."""


AUTH_CODES = frozenset({"unauthenticated", "key_locked"})
