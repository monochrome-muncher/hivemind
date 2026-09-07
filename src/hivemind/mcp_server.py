"""Re-export shim for the pre-wired ``hivemind-mcp`` console entry.

``pyproject.toml`` wires ``hivemind-mcp = hivemind.mcp_server:main``.
The server itself lives in the ``hivemind.mcp`` package (``app.py`` /
``server.py``); this module only re-exports the entry points so the
console script resolves without editing the shared ``pyproject.toml``.

If the orchestrator instead prefers to point the entry at
``hivemind.mcp.server:main``, this shim can be deleted.
"""

from __future__ import annotations

from hivemind.mcp.server import build_server, main

__all__ = ["build_server", "main"]