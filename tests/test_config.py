from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lune.config import (
    AppConfig,
    PersonaKernel,
    UserProfile,
    ensure_default_config,
    validate_private_setup,
)


def test_example_config_is_strict_and_private_safe() -> None:
    config = AppConfig.load(Path("examples/config.example.toml"))
    assert config.models.primary == "gpt-5.6-terra"
    assert config.privacy.store_openai_responses is False
    assert config.audio.barge_in_ms == 300


def test_example_persona_is_not_production_ready() -> None:
    persona = PersonaKernel.load(Path("examples/kernel.example.yaml"))
    assert persona.looks_like_example
    assert persona.boundaries.never_encourage_dependency


def test_example_persona_reports_unconfigured(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    persona_path = tmp_path / "kernel.yaml"
    config_path.write_bytes(Path("examples/config.example.toml").read_bytes())
    persona_path.write_bytes(Path("examples/kernel.example.yaml").read_bytes())
    assert validate_private_setup(config_path, persona_path) == ("persona_unconfigured",)


def test_config_rejects_unsafe_cloud_storage(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        Path("examples/config.example.toml")
        .read_text(encoding="utf-8")
        .replace("store_openai_responses = false", "store_openai_responses = true"),
        encoding="utf-8",
    )
    with pytest.raises(ValidationError):
        AppConfig.load(config_path)


def test_the_test_phase_default_provider_is_on_device() -> None:
    assert AppConfig().models.provider == "local_qwen"


def test_default_config_bootstraps_once_without_replacing_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "private" / "config.toml"

    assert ensure_default_config(path) is True
    assert AppConfig.load(path) == AppConfig()
    original = path.read_text(encoding="utf-8")

    assert ensure_default_config(path) is False
    assert path.read_text(encoding="utf-8") == original


def test_structured_persona_and_profile_round_trip_with_private_files(tmp_path: Path) -> None:
    persona = PersonaKernel.load(Path("examples/kernel.example.yaml"))
    persona_path = tmp_path / "persona" / "kernel.yaml"
    profile_path = tmp_path / "profile.toml"

    persona.save(persona_path)
    UserProfile(name="小林", context="喜歡在晚上散步。\n不喜歡被催促。").save(profile_path)

    assert PersonaKernel.load(persona_path) == persona
    assert UserProfile.load(profile_path).name == "小林"
    assert UserProfile.load(profile_path).context.endswith("不喜歡被催促。")
