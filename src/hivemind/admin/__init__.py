"""The admin panel runner (``hivemind-admin``, ADR 0029).

A static single-page UI plus a same-origin, allowlisted proxy to
``hivemind-api``. The browser holds the admin key; this process never
stores it, never logs it, and never talks to Postgres.
"""

from hivemind.admin.app import create_admin_app

__all__ = ["create_admin_app"]
