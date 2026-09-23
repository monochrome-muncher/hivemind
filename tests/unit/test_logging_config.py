"""``configure_logging`` (src/hivemind/config.py): every runner
(``api/main.py``, ``mcp/http.py``, ``mcp/server.py``) calls this once at
startup, driven by ``Settings.log_level`` / ``HIVEMIND_LOG_LEVEL``.

Without it, Python's root logger has no handler, so INFO-level logs
(retries, the startup line) go nowhere — WARNING+ still reaches stderr
via ``logging.lastResort``, but there is no consistent format and no way
to raise the floor to see more. An invalid level name is a startup
error, not a silent fallback.
"""

from __future__ import annotations

import logging

import pytest

from hivemind.config import Settings, configure_logging


@pytest.fixture(autouse=True)
def _reset_root_logger():
    """``basicConfig`` is a process-wide, one-shot side effect (it no-ops
    if handlers already exist) — strip them so each test starts clean."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers[:] = original_handlers
    root.setLevel(original_level)


class TestConfigureLogging:
    def test_sets_the_root_logger_level(self) -> None:
        root = logging.getLogger()
        root.handlers.clear()
        configure_logging("WARNING")
        assert root.level == logging.WARNING

    def test_is_case_insensitive(self) -> None:
        root = logging.getLogger()
        root.handlers.clear()
        configure_logging("debug")
        assert root.level == logging.DEBUG

    def test_default_is_info(self) -> None:
        root = logging.getLogger()
        root.handlers.clear()
        configure_logging()
        assert root.level == logging.INFO

    def test_an_unknown_level_name_raises_loudly(self) -> None:
        """Not a silent fallback to WARNING: a typo'd HIVEMIND_LOG_LEVEL
        must fail startup, the same posture ADR 0024 takes on unknown
        env var NAMES — this is the same discipline applied to a value."""
        with pytest.raises(ValueError, match="HIVEMIND_LOG_LEVEL"):
            configure_logging("BOGUS")

    def test_settings_field_default_matches(self) -> None:
        assert Settings.model_fields["log_level"].default == "INFO"
