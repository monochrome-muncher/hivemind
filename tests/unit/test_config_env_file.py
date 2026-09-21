"""Tests for ENVIRONMENT-selected environment profiles (ADR 0017).

``load_settings()`` reads the ``ENVIRONMENT`` env var and builds
``Settings`` from the matching per-environment dotenv profile file:
``production`` → ``.env.production``, ``staging`` → ``.env.staging``,
``test`` → ``.env.test``, anything else (or unset) → ``.env.local``.
Real ``HIVEMIND_*`` environment variables always win over file values,
and a missing file is silently ignored (the Kubernetes / CI posture:
values come from env / Secrets and no profile file exists).

Hermetic: each test chdir's into a scratch directory AND explicitly
removes/sets the relevant env vars, so the assertions hold both under
``make test`` (which exports a dev env, e.g. ``HIVEMIND_EMBEDDING_DIM=512``)
and under a bare ``uv run pytest`` (no env, pure defaults).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivemind.config import Settings, load_settings


def _write_env(path: Path, values: list[str]) -> None:
    path.write_text("\n".join(values) + "\n")


def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop any inherited env for the fields these tests touch."""
    for name in (
        "HIVEMIND_EMBEDDING_DIM",
        "HIVEMIND_EMBEDDING_MODEL",
        "HIVEMIND_EXTRACTOR_ENDPOINT",
        "HIVEMIND_QUALITY_MIN",
        "ENVIRONMENT",
    ):
        monkeypatch.delenv(name, raising=False)


# --- the ENVIRONMENT → profile-file mapping ---------------------------------


def test_production_profile_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(tmp_path / ".env.production", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.embedding_dim == 768


def test_staging_profile_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(tmp_path / ".env.staging", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.embedding_dim == 768


def test_test_profile_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(tmp_path / ".env.test", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.embedding_dim == 768


def test_unset_environment_falls_back_to_local_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path / ".env.local", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)  # ENVIRONMENT removed
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.embedding_dim == 768


def test_unknown_environment_falls_back_to_local_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path / ".env.local", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "qa")  # not one of the three known names
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.embedding_dim == 768


def test_environment_name_is_case_and_whitespace_insensitive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path / ".env.production", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "  Production ")
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.embedding_dim == 768


# --- precedence + the missing-file posture ---------------------------------


def test_real_env_vars_beats_profile_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(tmp_path / ".env.production", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("HIVEMIND_EMBEDDING_DIM", "512")  # real env beats the file
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.embedding_dim == 512


def test_missing_profile_file_is_silently_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ENVIRONMENT names a profile whose file does not exist: the declared
    # defaults apply and nothing raises (the k8s/CI posture).
    _clean_env(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.embedding_dim == 1024  # the declared default (ADR 0015)
    assert settings.extractor_endpoint == ""  # unset = extraction off (ADR 0016)


def test_profile_file_only_sets_fields_it_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path / ".env.local", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.extractor_endpoint == ""  # still the default: extraction off
    assert settings.quality_min == 0.5  # an untouched field keeps its default


# --- the class default (bare Settings()) + the retired .env -----------------


def test_bare_settings_uses_the_local_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_env(tmp_path / ".env.local", ["HIVEMIND_EMBEDDING_DIM=768"])
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.embedding_dim == 768


def test_retired_bare_env_file_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The old quickstart (cp .env.example .env) is retired (ADR 0017): a
    # stray bare `.env` must NOT leak values into Settings.
    _write_env(tmp_path / ".env", ["HIVEMIND_EMBEDDING_DIM=999"])
    _clean_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    settings = Settings()
    assert settings.embedding_dim == 1024  # the default, not the .env value
