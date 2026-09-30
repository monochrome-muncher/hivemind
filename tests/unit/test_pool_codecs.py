"""``make_pool``: the jsonb codec and the timeout knobs (no live Postgres)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from hivemind.config import Settings
from hivemind.store.pool import PoolTimeouts, make_pool


async def test_init_registers_a_jsonb_codec_that_decodes_but_does_not_reencode() -> None:
    """jsonb reads decode to native Python; writes stay as the caller's
    already-dumped JSON text (PgStore ``json.dumps`` + ``$n::jsonb``), so
    the codec must not double-encode."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(dsn: str, **kwargs: Any) -> None:
        captured.update(kwargs)

    with patch("hivemind.store.pool.asyncpg.create_pool", new=fake_create_pool):
        await make_pool("postgresql://example/db")

    conn = MagicMock()
    conn.set_type_codec = AsyncMock()
    with patch("hivemind.store.pool.register_vector", new=AsyncMock()):
        await captured["init"](conn)
    calls = {c.args[0]: c.kwargs for c in conn.set_type_codec.call_args_list}
    assert {"json", "jsonb"} <= calls.keys()
    jsonb = calls["jsonb"]
    assert jsonb["schema"] == "pg_catalog"
    assert jsonb["decoder"]('{"n": 1}') == {"n": 1}
    assert jsonb["encoder"]('{"n": 1}') == '{"n": 1}'


async def test_timeouts_reach_create_pool() -> None:
    with patch("hivemind.store.pool.asyncpg.create_pool", new=AsyncMock()) as create_pool:
        await make_pool("postgresql://example/db", command_timeout=12.0, statement_timeout_ms=3000)
    kwargs = create_pool.call_args.kwargs
    assert kwargs["command_timeout"] == 12.0
    assert kwargs["server_settings"] == {"statement_timeout": "3000"}


async def test_no_statement_timeout_means_no_server_settings() -> None:
    """Zero disables the server-side bound entirely."""
    with patch("hivemind.store.pool.asyncpg.create_pool", new=AsyncMock()) as create_pool:
        await make_pool("postgresql://example/db", statement_timeout_ms=0)
    assert "server_settings" not in create_pool.call_args.kwargs


async def test_defaults_send_no_startup_parameters() -> None:
    """The org runs Postgres behind a transaction-mode PgBouncer, which by
    default REJECTS unknown startup parameters (``unsupported startup
    parameter: statement_timeout``) and, even when told to ignore one,
    never applies it to the shared server connections. So the server-side
    bound is opt-in: by default nothing but the client-side
    ``command_timeout`` bounds a call, and the startup packet is plain."""
    with patch("hivemind.store.pool.asyncpg.create_pool", new=AsyncMock()) as create_pool:
        await make_pool("postgresql://example/db")
    kwargs = create_pool.call_args.kwargs
    assert "server_settings" not in kwargs
    assert kwargs["command_timeout"] == 30.0

    assert PoolTimeouts().statement_ms == 0
    assert Settings(_env_file=None).pool_statement_timeout_ms == 0  # type: ignore[call-arg]
