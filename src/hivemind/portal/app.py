"""The agent portal ASGI app (ADR 0095): a ``hivemind.webapp`` with the
portal's static UI, the admin panel's shared front-end modules (ADR 0091)
and the portal allowlist.

Only what an agent key may read, the portal's own uncounted search, its
audited withdraw, pin and unpin, its clean-up of the agent's own work and its
key lock (ADR 0117) are reachable: no write of the data plane, no feedback,
nothing of the admin surface.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from starlette.applications import Starlette

from hivemind.admin.app import STATIC_DIR as ADMIN_STATIC_DIR
from hivemind.webapp import allowlist, allows, create_proxy_app

__all__ = ["SHARED_MODULES", "STATIC_DIR", "create_portal_app", "is_allowed"]

STATIC_DIR = Path(__file__).parent / "static"

# The admin panel files the portal serves as they are: the generic
# modules (they import nothing admin-specific, ADR 0091), the stylesheet
# and the icon. Nothing else of the admin folder is reachable.
SHARED_MODULES = (
    "ui.js",
    "client.js",
    "lists.js",
    "csv.js",
    "drawer.js",
    "charts.js",
    "admin.css",
    "favicon.svg",
)

# (method, path-regex) pairs the proxy forwards; everything else is 404.
# Anchored: the path is matched whole, after the `/v1/` prefix.
_ALLOWED = allowlist(
    (
        ("GET", r"health"),
        ("GET", r"whoami"),
        # One entry in full (with ?history=true) and the home fleet's pins:
        # the data plane's own reads, which the key's visibility bounds.
        ("GET", r"entries/[^/]+"),
        ("GET", r"pins"),
        # The portal's own routes (agent keys only on the API side).
        ("GET", r"portal/entries"),
        ("POST", r"portal/search"),
        ("GET", r"portal/feedback"),
        ("GET", r"portal/flagged"),
        ("POST", r"portal/entries/[^/]+/withdraw"),
        ("PUT", r"portal/entries/[^/]+/pin"),
        ("DELETE", r"portal/entries/[^/]+/pin"),
        # The agent's own clean-up over a time window (ADR 0118).
        ("POST", r"portal/cleanup"),
        ("GET", r"portal/cleanups"),
        ("POST", r"portal/cleanups/[^/]+/undo"),
        # "This key leaked": locks the signed-in key, nothing else.
        ("POST", r"portal/key/lock"),
    )
)


def is_allowed(method: str, path: str) -> bool:
    """Whether the proxy forwards ``method /v1/<path>`` (ADR 0095)."""
    return allows(_ALLOWED, method, path)


def create_portal_app(
    api_url: str,
    *,
    verify: bool | str = True,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 30.0,
) -> Starlette:
    """Build the agent portal app, proxying to ``api_url``."""
    return create_proxy_app(
        api_url,
        allowed=_ALLOWED,
        static_dir=STATIC_DIR,
        shared_static=(ADMIN_STATIC_DIR, SHARED_MODULES),
        name="the agent portal",
        api_url_setting="HIVEMIND_PORTAL_API_URL",
        verify=verify,
        transport=transport,
        timeout=timeout,
    )
