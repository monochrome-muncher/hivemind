"""The agent portal runner (``hivemind-portal``, ADR 0095): a static UI and an allowlisted proxy to
``hivemind-api``.

The browser holds the agent key; this process never stores or logs it and never talks to Postgres.
"""

from hivemind.portal.app import create_portal_app

__all__ = ["create_portal_app"]
