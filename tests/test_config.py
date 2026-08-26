from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from lune.config import AppConfig, PersonaKernel, validate_private_setup


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
