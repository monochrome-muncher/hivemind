"""Serve the real HTTP MCP runner for the client packages' live tests.

Run from the repository root against a migrated, throwaway database:

    uv run python clients/live_server.py --port 8765 --env-file /tmp/hivemind-live.env

It registers a contributor agent in a fresh fleet, writes ``HIVEMIND_MCP_URL`` and
``HIVEMIND_API_KEY`` to ``--env-file`` (mode 0600), then serves on 127.0.0.1 with the local hash
embedder, so no embedding endpoint is needed. Each run uses new names, so it can be re-run on the
same database.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid
from pathlib import Path

import uvicorn

from hivemind.config import Settings, load_settings
from hivemind.domain.access import TrustLevel
from hivemind.mcp.http import build_http_app
from hivemind.mcp.local_embedder import LocalEmbedder
from hivemind.store import build_authenticator, build_store
from hivemind.store.keys import _issue_agent


async def _contributor_key(settings: Settings) -> str:
    suffix = uuid.uuid4().hex[:8]
    store = build_store(settings)
    try:
        fleet = await store.create_fleet(f"clients-live-{suffix}")
        agent = await store.register_agent(f"client-live-{suffix}", "ci")
    finally:
        await store.close()
    return await _issue_agent(
        settings.database_url,
        agent.name,
        trust_level=TrustLevel.CONTRIBUTOR,
        home_fleet_id=fleet.id,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the HTTP MCP runner for live tests.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()

    os.environ["HIVEMIND_HOST"] = "127.0.0.1"
    settings = load_settings()
    key = asyncio.run(_contributor_key(settings))
    args.env_file.write_text(
        f"HIVEMIND_MCP_URL=http://127.0.0.1:{args.port}/mcp\nHIVEMIND_API_KEY={key}\n"
    )
    args.env_file.chmod(0o600)

    app = build_http_app(
        settings,
        build_store(settings),
        LocalEmbedder(settings.embedding_dim),
        build_authenticator(settings),
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
