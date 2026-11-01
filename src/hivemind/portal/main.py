"""Console entry point for the agent portal (``hivemind-portal``, ADR 0095)."""

from __future__ import annotations

import logging

import uvicorn

from hivemind.config import bind_host, bind_port, configure_logging, load_settings
from hivemind.portal.app import create_portal_app

logger = logging.getLogger(__name__)


def run() -> None:
    """Serve the agent portal. Needs only ``HIVEMIND_PORTAL_API_URL``."""
    settings = load_settings()
    configure_logging(settings.log_level)
    if not settings.portal_api_url:
        raise SystemExit(
            "hivemind-portal: set HIVEMIND_PORTAL_API_URL to the hivemind-api base URL "
            "(e.g. http://hivemind-api:8000)"
        )
    verify: bool | str = settings.portal_api_ca_bundle or True
    logger.info(
        "starting hivemind-portal: api_url=%s ca_bundle=%s",
        settings.portal_api_url,
        settings.portal_api_ca_bundle or "system",
    )
    uvicorn.run(
        create_portal_app(settings.portal_api_url, verify=verify),
        host=bind_host(),
        port=bind_port(8080),
    )
