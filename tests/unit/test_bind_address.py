"""The shared ``HIVEMIND_HOST`` / ``HIVEMIND_PORT`` readers every HTTP runner uses."""

from __future__ import annotations

import pytest

from hivemind.config import bind_host, bind_port


def test_defaults_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIVEMIND_HOST", raising=False)
    monkeypatch.delenv("HIVEMIND_PORT", raising=False)
    assert bind_host() == "0.0.0.0"
    assert bind_port(8080) == 8080
    assert bind_port(8000) == 8000


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEMIND_HOST", "127.0.0.1")
    monkeypatch.setenv("HIVEMIND_PORT", "9123")
    assert bind_host() == "127.0.0.1"
    assert bind_port(8080) == 9123


def test_invalid_port_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIVEMIND_PORT", "http")
    with pytest.raises(ValueError):
        bind_port(8000)
