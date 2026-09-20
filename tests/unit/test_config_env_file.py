"""Tests for the local ``.env`` quickstart (DEPLOY.md §2: ``cp .env.example .env``).

The ``Settings`` class loads ``HIVEMIND_*`` values from a local ``.env``
file when one exists (pydantic-settings ``env_file``), with real
environment variables always beating ``.env`` values. A missing
``.env`` is silently ignored, so Kubernetes / CI (no ``.env`` file,
values come from env / Secrets) are unaffected.

Hermetic: each test chdir's into a scratch directory AND explicitly
removes/sets the relevant ``HIVEMIND_*`` env vars, so the assertions
hold both under ``make test`` (which exports a dev env, e.g.
``HIVEMIND_EMBEDDING_DIM=512``) and under a bare ``uv run pytest``
(no env, pure defaults).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivemind.config import Settings


def _write_env(path: Path, values: list[str]) -> None:
    path.write_text("\n".join(values) + "\n")


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any inherited env for the fields these tests touch."""
    for name in (
        "HIVEMIND_EMBEDDING_DIM",
        "HIVEMIND_EMBEDDING_MODEL",
        "HIVEMIND_EXTRACTOR_ENDPOINT",
        "HIVEMIND_QUALITY_MIN",
    ):
        monkeypatch.delenv(name, raising=False)


def test_env_file_values_are_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(
        tmp_path / ".env",
        ["HIVEMIND_EMBEDDING_DIM=768", "HIVEMIND_EMBEDDING_MODEL=from-dotenv"],
    )
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.embedding_dim == 768
    assert settings.embedding_model == "from-dotenv"


def test_missing_env_file_is_silently_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A directory with NO .env file: Settings() falls back to the
    # declared defaults and raises nothing (the k8s/CI posture).
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.embedding_dim == 1024  # the declared default (ADR 0015)
    assert settings.extractor_endpoint == ""  # unset = extraction off (ADR 0016)


def test_real_env_vars_override_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(tmp_path / ".env", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.setenv("HIVEMIND_EMBEDDING_DIM", "512")  # real env beats .env
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.embedding_dim == 512


def test_env_file_only_sets_fields_it_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path / ".env", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.extractor_endpoint == ""  # still the default: extraction stays off
    assert settings.quality_min == 0.5  # an untouched field keeps its default
