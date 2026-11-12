"""The agent portal ASGI app (ADR 0095): ``hivemind.webapp`` with the portal UI, the admin panel's
shared modules (ADR 0091) and the portal allowlist.

Reachable: what an agent key may read, the portal's own routes and the key lock (ADR 0117). No
data-plane write, no feedback, nothing of the admin surface.
"""

from __future__ import annotations

from pathlib import Path

import httpx
from starlette.applications import Starlette

from hivemind.admin.app import STATIC_DIR as ADMIN_STATIC_DIR
from hivemind.webapp import allowlist, allows, create_proxy_app

__all__ = ["SHARED_MODULES", "STATIC_DIR", "create_portal_app", "is_allowed"]

STATIC_DIR = Path(__file__).parent / "static"

# Admin panel files served as they are (generic modules, ADR 0091, plus stylesheet, icon, font);
# nothing else of that folder.
SHARED_MODULES = (
    "ui.js",
    "client.js",
    "lists.js",
    "memfilters.js",
    "csv.js",
    "drawer.js",
    "charts.js",
    "explain.js",
    "since.js",
    "admin.css",
    "favicon.svg",
    "outfit.woff2",
)

# (method, path-regex) pairs forwarded; everything else is 404. Matched whole, after `/v1/`.
_ALLOWED = allowlist(
    (
        ("GET", r"health"),
        ("GET", r"whoami"),
        # The data plane's own reads (one entry with ?history, the fleet's pins), bounded by the
        # key's visibility.
        ("GET", r"entries/[^/]+"),
        ("GET", r"pins"),
        # The portal's own routes.
        ("GET", r"portal/entries"),
        ("POST", r"portal/search"),
        ("GET", r"portal/feedback"),
        ("GET", r"portal/flagged"),
        ("GET", r"portal/relied-on"),
        ("POST", r"portal/entries/[^/]+/withdraw"),
        ("PUT", r"portal/entries/[^/]+/pin"),
        ("DELETE", r"portal/entries/[^/]+/pin"),
        # Agent self-service cleanup (ADR 0118).
        ("POST", r"portal/cleanup"),
        ("GET", r"portal/cleanups"),
        ("POST", r"portal/cleanups/[^/]+/undo"),
        # History page (ADR 0123).
        ("GET", r"portal/history"),
        # Locks the signed-in key, nothing else.
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
