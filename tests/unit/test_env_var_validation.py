"""Tests for the unknown-``HIVEMIND_*``-variable rejection (ADR 0024).

``Settings`` keeps ``extra="ignore"`` (a naive ``extra="forbid"`` would
crash-loop every container — three allowlisted variables are set on
every pod). The check instead lives in ``load_settings()``, the real
deployment entry point, and runs against both real process env vars and
the active ``ENVIRONMENT`` profile file (ADR 0017).

Hermetic like ``test_config_env_file.py``: every test clears ALL
``HIVEMIND_*`` / ``ENVIRONMENT`` env vars first and chdir's into a
scratch directory, so ambient dev-shell or CI exports never leak in.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivemind.config import _ALLOWED_EXTRA_ENV_VARS, load_settings


def _clear_hivemind_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drop every HIVEMIND_* / ENVIRONMENT var this process inherited."""
    import os

    for name in list(os.environ):
        if name.startswith("HIVEMIND_") or name == "ENVIRONMENT":
            monkeypatch.delenv(name, raising=False)


def test_unknown_variable_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVEMIND_NONSENSE", "1")
    with pytest.raises(RuntimeError, match="HIVEMIND_NONSENSE"):
        load_settings()


def test_unknown_variable_error_names_it_without_a_bogus_suggestion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name with no close relative gets no "did you mean" (regression:
    matching on the full name let the shared HIVEMIND_ prefix alone push
    unrelated names like RUNNER over difflib's similarity cutoff)."""
    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVEMIND_NONSENSE", "1")
    with pytest.raises(RuntimeError) as exc_info:
        load_settings()
    assert "did you mean" not in str(exc_info.value)


def test_renamed_variable_suggests_its_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ADR 0021 rename (PREFIX_CHARS -> PREFIX_TOKENS) is exactly the
    failure mode this check exists for: the old name now fails loudly
    and points at the new one, instead of silently keeping the default.
    """
    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVEMIND_EMBEDDING_PREFIX_CHARS", "500")
    with pytest.raises(RuntimeError, match="HIVEMIND_EMBEDDING_PREFIX_TOKENS"):
        load_settings()


def test_multiple_unknown_variables_are_all_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVEMIND_FOO", "1")
    monkeypatch.setenv("HIVEMIND_BAR", "2")
    with pytest.raises(RuntimeError) as exc_info:
        load_settings()
    assert "HIVEMIND_FOO" in str(exc_info.value)
    assert "HIVEMIND_BAR" in str(exc_info.value)


@pytest.mark.parametrize("name", sorted(_ALLOWED_EXTRA_ENV_VARS))
def test_each_allowlisted_variable_is_accepted_alone(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(name, "some-value")
    load_settings()  # must not raise


def test_realistic_production_environment_loads_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact posture the task calls out: HIVEMIND_RUNNER / HOST / PORT
    (all non-Settings-field, pod-wide variables) alongside real settings.
    """
    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVEMIND_RUNNER", "api")
    monkeypatch.setenv("HIVEMIND_HOST", "0.0.0.0")
    monkeypatch.setenv("HIVEMIND_PORT", "8000")
    monkeypatch.setenv(
        "HIVEMIND_DATABASE_URL", "postgresql://hivemind:hivemind@localhost:5432/hivemind"
    )
    monkeypatch.setenv("HIVEMIND_EMBEDDING_ENDPOINT", "http://localhost:8001/v1")
    monkeypatch.setenv("HIVEMIND_EMBEDDING_DIM", "512")
    settings = load_settings()
    assert settings.embedding_dim == 512


def test_a_known_settings_field_env_var_is_never_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVEMIND_QUALITY_MIN", "0.3")
    settings = load_settings()
    assert settings.quality_min == 0.3


def test_pool_max_size_env_var_is_accepted_and_reaches_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The new ADR-0024-covered fields (``pool_min_size``/``pool_max_size``,
    the multi-replica-readiness pool-size knobs) are real ``Settings``
    fields, so their ``HIVEMIND_*`` spellings must load, not be rejected
    as unknown."""
    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "HIVEMIND_DATABASE_URL", "postgresql://hivemind:hivemind@localhost:5432/hivemind"
    )
    monkeypatch.setenv("HIVEMIND_POOL_MAX_SIZE", "20")
    settings = load_settings()
    assert settings.pool_max_size == 20


def test_unknown_variable_in_the_profile_file_is_also_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in the profile file (ADR 0017) is the same silent no-op as
    a typo in a real env var, so it gets the same loud rejection."""
    _clear_hivemind_env(monkeypatch)
    (tmp_path / ".env.local").write_text("HIVEMIND_TOTALLY_MADE_UP=1\n")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="HIVEMIND_TOTALLY_MADE_UP"):
        load_settings()


def test_bare_settings_construction_is_never_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check lives in load_settings(), not on the Settings model: a
    bare Settings() (as most of the test suite and dev scripts use) is
    unaffected by stray HIVEMIND_* vars in the ambient environment."""
    from hivemind.config import Settings

    _clear_hivemind_env(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HIVEMIND_NONSENSE", "1")
    Settings()  # must not raise
