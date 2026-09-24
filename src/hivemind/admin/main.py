"""Console entry point for the admin panel (``hivemind-admin``, ADR 0029)."""

from __future__ import annotations

import logging
import os

import uvicorn

from hivemind.admin.app import create_admin_app
from hivemind.config import configure_logging, load_settings

logger = logging.getLogger(__name__)


def run() -> None:
    """Serve the admin panel. Needs only ``HIVEMIND_ADMIN_API_URL``."""
    settings = load_settings()
    configure_logging(settings.log_level)
    if not settings.admin_api_url:
        raise SystemExit(
            "hivemind-admin: set HIVEMIND_ADMIN_API_URL to the hivemind-api base URL "
            "(e.g. http://hivemind-api:8000)"
        )
    verify: bool | str = settings.admin_api_ca_bundle or True
    logger.info(
        "starting hivemind-admin: api_url=%s ca_bundle=%s",
        settings.admin_api_url,
        settings.admin_api_ca_bundle or "system",
    )
    uvicorn.run(
        create_admin_app(settings.admin_api_url, verify=verify),
        host=os.environ.get("HIVEMIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("HIVEMIND_PORT", "8080")),
    )
