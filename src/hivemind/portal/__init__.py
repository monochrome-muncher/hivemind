"""The agent portal runner (``hivemind-portal``, ADR 0095).

A static single-page UI plus a same-origin, allowlisted proxy to
``hivemind-api``, for a person holding an agent key: the agent's
memories, its verdicts, its home fleet and search. The browser holds the
key; this process never stores it, never logs it, and never talks to
Postgres.
"""

from hivemind.portal.app import create_portal_app

__all__ = ["create_portal_app"]
