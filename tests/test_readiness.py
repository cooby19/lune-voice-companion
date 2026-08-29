from __future__ import annotations

import stat
from pathlib import Path

import keyring.errors
import pytest

from lune.config import AppConfig
from lune.paths import LunePaths
from lune.readiness import check_readiness


def write_config(paths: LunePaths, provider: str) -> None:
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    paths.config.write_text(
        f'schema_version = 1\n[models]\nprovider = "{provider}"\n',
        encoding="utf-8",
    )


def test_keychain_failure_is_setup_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    write_config(paths, "openai_responses")

    def fail() -> str | None:
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr("lune.readiness.get_openai_api_key", fail)
    readiness = check_readiness(paths)
    assert readiness.state == "setup_required"
    assert "keychain_unavailable" in readiness.reasons


def test_the_on_device_composition_asks_for_the_artifact_not_a_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    write_config(paths, "local_qwen")

    def fail() -> str | None:
        raise AssertionError("the on-device composition must never read the Keychain")

    monkeypatch.setattr("lune.readiness.get_openai_api_key", fail)
    readiness = check_readiness(paths)

    assert readiness.state == "setup_required"
    assert "api_key_missing" not in readiness.reasons
    assert "local_llm_model_missing" in readiness.reasons
    assert "local_llm_runtime_missing" in readiness.reasons


def test_missing_config_is_created_before_setup_reasons_are_reported(tmp_path: Path) -> None:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")

    readiness = check_readiness(paths)

    assert paths.config.is_file()
    assert "config_missing" not in readiness.reasons
    # Every field has a safe default, so the written file must load back as one
    # and must stay as private as anything else the app writes.
    assert AppConfig.load(paths.config) == AppConfig()
    assert stat.S_IMODE(paths.config.stat().st_mode) == 0o600


def test_an_unreadable_config_is_reported_instead_of_being_overwritten(tmp_path: Path) -> None:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    edited = "schema_version = 1\n[budget]\nfallback_at_twd = 900.0\nlock_at_twd = 700.0\n"
    paths.config.write_text(edited, encoding="utf-8")

    readiness = check_readiness(paths)

    assert "config_invalid" in readiness.reasons
    assert "config_missing" not in readiness.reasons
    # Self-repair may create a file, never replace one the user has edited.
    assert paths.config.read_text(encoding="utf-8") == edited


def test_a_malformed_config_is_reported_instead_of_being_overwritten(tmp_path: Path) -> None:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")
    paths.config.parent.mkdir(parents=True, exist_ok=True)
    broken = "schema_version = = 1\n"
    paths.config.write_text(broken, encoding="utf-8")

    readiness = check_readiness(paths)

    assert "config_invalid" in readiness.reasons
    assert paths.config.read_text(encoding="utf-8") == broken
