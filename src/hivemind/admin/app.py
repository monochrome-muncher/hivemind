"""The admin panel ASGI app (ADR 0029): a ``hivemind.webapp`` with the
admin static UI and the admin allowlist.

Only the admin surface, the metrics counters and the health probe are
reachable: the panel is never a relay onto the data plane.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from starlette.applications import Starlette

from hivemind.webapp import (
    CONTENT_SECURITY_POLICY,
    SECURITY_HEADERS,
    allowlist,
    allows,
    create_proxy_app,
)

__all__ = [
    "CONTENT_SECURITY_POLICY",
    "SECURITY_HEADERS",
    "STATIC_DIR",
    "create_admin_app",
    "is_allowed",
]

STATIC_DIR = Path(__file__).parent / "static"

# (method, path-regex) pairs the proxy forwards; everything else is 404.
# Anchored: the path is matched whole, after the `/v1/` prefix.
_ALLOWED = allowlist(
    (
        ("GET", r"health"),
        ("GET", r"metrics"),
        ("GET", r"admin/agents"),
        ("GET", r"admin/fleets"),
        ("POST", r"admin/fleets"),
        ("GET", r"admin/audit-log"),
        ("POST", r"admin/agents/[^/]+/activate"),
        ("POST", r"admin/agents/[^/]+/revoke"),
        ("POST", r"admin/agents/[^/]+/key"),
        ("PATCH", r"admin/agents/[^/]+"),
        ("POST", r"admin/org-key/rotate"),
        # Browsing memories as the operator (admin-key only on the API side).
        # Reads and one withdraw: never the data plane's own entry routes.
        ("GET", r"admin/entries"),
        ("GET", r"admin/entries/[^/]+"),
        ("POST", r"admin/entries/[^/]+/withdraw"),
        ("POST", r"admin/search"),
        ("GET", r"admin/activity"),
        ("GET", r"admin/feedback"),
        ("GET", r"admin/fleets/[^/]+/pins"),
        ("GET", r"admin/flagged"),
        # Merging one fleet into another, and undoing it (admin-key only on the API side).
        ("POST", r"admin/fleets/[^/]+/merge"),
        # Renaming a fleet (admin-key only on the API side).
        ("PATCH", r"admin/fleets/[^/]+"),
        ("POST", r"admin/fleets/[^/]+/move"),
        ("GET", r"admin/fleet-merges"),
        ("POST", r"admin/fleet-merges/[^/]+/undo"),
    )
)


def is_allowed(method: str, path: str) -> bool:
    """Whether the proxy forwards ``method /v1/<path>`` (ADR 0029)."""
    return allows(_ALLOWED, method, path)


def create_admin_app(
    api_url: str,
    *,
    verify: bool | str = True,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> Starlette:
    """Build the admin panel app, proxying to ``api_url``."""
    return create_proxy_app(
        api_url,
        allowed=_ALLOWED,
        static_dir=STATIC_DIR,
        name="the admin panel",
        api_url_setting="HIVEMIND_ADMIN_API_URL",
        verify=verify,
        transport=transport,
        timeout=timeout,
    )
