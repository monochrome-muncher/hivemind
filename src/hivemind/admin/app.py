"""The admin panel ASGI app (ADR 0029): ``hivemind.webapp`` with the admin UI and allowlist.

Only the admin surface, metrics and health are reachable: never a relay onto the data plane.
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

# (method, path-regex) pairs forwarded; everything else is 404. Matched whole, after `/v1/`.
_ALLOWED = allowlist(
    (
        ("GET", r"health"),
        ("GET", r"metrics"),
        ("GET", r"admin/agents"),
        ("POST", r"admin/agents"),
        ("GET", r"admin/fleets"),
        ("POST", r"admin/fleets"),
        ("GET", r"admin/audit-log"),
        ("POST", r"admin/agents/[^/]+/activate"),
        ("POST", r"admin/agents/[^/]+/revoke"),
        ("POST", r"admin/agents/[^/]+/key"),
        ("PATCH", r"admin/agents/[^/]+"),
        ("PUT", r"admin/agents/[^/]+/role"),
        ("DELETE", r"admin/agents/[^/]+/role"),
        ("POST", r"admin/org-key/rotate"),
        # Memory browser: reads, withdraw, restore, redact; never the data plane's entry routes.
        ("GET", r"admin/entries"),
        ("GET", r"admin/entries/[^/]+"),
        ("POST", r"admin/entries/[^/]+/withdraw"),
        ("POST", r"admin/entries/[^/]+/restore"),
        ("POST", r"admin/entries/[^/]+/redact"),  # ADR 0125
        ("POST", r"admin/search"),
        ("GET", r"admin/activity"),
        ("GET", r"admin/feedback"),
        ("GET", r"admin/fleets/[^/]+/pins"),
        ("GET", r"admin/flagged"),
        # Fleet merge, rename, move and phase (ADRs 0107, 0109, 0112, 0143).
        ("POST", r"admin/fleets/[^/]+/merge"),
        ("PATCH", r"admin/fleets/[^/]+"),
        ("PUT", r"admin/fleets/[^/]+/phase"),
        ("DELETE", r"admin/fleets/[^/]+/phase"),
        ("POST", r"admin/fleets/[^/]+/move"),
        ("GET", r"admin/fleet-merges"),
        ("POST", r"admin/fleet-merges/[^/]+/undo"),
        # Successions (ADR 0113).
        ("POST", r"admin/agents/[^/]+/retire"),
        ("GET", r"admin/successions"),
        ("POST", r"admin/successions/[^/]+/undo"),
        # Offboarding an owner (ADR 0135).
        ("POST", r"admin/owners/[^/].*/offboard"),
        # Agent cleanups (ADR 0111).
        ("POST", r"admin/agents/[^/]+/cleanup"),
        ("GET", r"admin/agent-cleanups"),
        ("POST", r"admin/agent-cleanups/[^/]+/undo"),
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
