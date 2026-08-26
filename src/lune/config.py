"""Strict public configuration and private persona loading."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelsConfig(StrictModel):
    primary: Literal["gpt-5.6-terra"] = "gpt-5.6-terra"
    fallback: Literal["gpt-5.6-luna"] = "gpt-5.6-luna"
    reasoning_effort: Literal["none"] = "none"
    max_output_tokens: int = Field(default=192, ge=1, le=192)


class AudioConfig(StrictModel):
    sample_rate: Literal[16000] = 16000
    channels: Literal[1] = 1
    turn_start_ms: Literal[100] = 100
    barge_in_ms: Literal[300] = 300
    pre_roll_ms: int = Field(default=350, ge=350)
    end_silence_ms: Literal[350] = 350


class BudgetConfig(StrictModel):
    timezone: Literal["Asia/Taipei"] = "Asia/Taipei"
    twd_per_usd: float = Field(default=33.0, gt=0)
    fallback_at_twd: float = Field(default=700.0, ge=0)
    lock_at_twd: float = Field(default=900.0, ge=0)

    @model_validator(mode="after")
    def ordered_thresholds(self) -> BudgetConfig:
        if self.fallback_at_twd >= self.lock_at_twd:
            raise ValueError("fallback threshold must be lower than lock threshold")
        return self


class PrivacyConfig(StrictModel):
    store_openai_responses: Literal[False] = False
    telemetry: Literal[False] = False
    tracing: Literal[False] = False


class TTSConfig(StrictModel):
    preferred_backend: Literal["avspeech", "gpt_sovits"] = "avspeech"
    gpt_worker_python: str = "/usr/local/bin/python3.10"


class AppConfig(StrictModel):
    schema_version: Literal[1] = 1
    models: ModelsConfig = ModelsConfig()
    audio: AudioConfig = AudioConfig()
    budget: BudgetConfig = BudgetConfig()
    privacy: PrivacyConfig = PrivacyConfig()
    tts: TTSConfig = TTSConfig()

    @classmethod
    def load(cls, path: Path) -> AppConfig:
        with path.open("rb") as handle:
            return cls.model_validate(tomllib.load(handle))


class Identity(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    presentation: str = Field(min_length=1, max_length=80)
    user_address: str = Field(min_length=1, max_length=80)


class Language(StrictModel):
    primary: str = "zh-TW"
    chinese_ratio: float = Field(default=0.8, ge=0, le=1)


class SentenceBounds(StrictModel):
    min: int = Field(default=1, ge=1, le=3)
    max: int = Field(default=3, ge=1, le=3)


class Style(StrictModel):
    traits: tuple[str, ...]
    default_sentences: SentenceBounds


class Boundaries(StrictModel):
    never_claim_human: Literal[True] = True
    never_encourage_dependency: Literal[True] = True
    admit_uncertainty: Literal[True] = True
    respect_user_agency: Literal[True] = True


class Proactivity(StrictModel):
    level: str
    no_scheduled_external_messages: Literal[True] = True


class PersonaKernel(StrictModel):
    schema_version: Literal[1] = 1
    identity: Identity
    language: Language
    style: Style
    boundaries: Boundaries
    proactivity: Proactivity

    @model_validator(mode="after")
    def sentence_bounds_are_ordered(self) -> PersonaKernel:
        if self.style.default_sentences.min > self.style.default_sentences.max:
            raise ValueError("sentence minimum cannot exceed maximum")
        return self

    @classmethod
    def load(cls, path: Path) -> PersonaKernel:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return cls.model_validate(raw)

    @property
    def looks_like_example(self) -> bool:
        return self.identity.user_address.strip().casefold() == "friend"


def validate_private_setup(config_path: Path, persona_path: Path) -> tuple[str, ...]:
    """Return opaque setup reason codes without leaking private content."""

    reasons: list[str] = []
    try:
        AppConfig.load(config_path)
    except FileNotFoundError:
        reasons.append("config_missing")
    except (OSError, tomllib.TOMLDecodeError, ValidationError):
        reasons.append("config_invalid")
    try:
        persona = PersonaKernel.load(persona_path)
    except FileNotFoundError:
        reasons.append("persona_missing")
    except (OSError, yaml.YAMLError, ValidationError):
        reasons.append("persona_invalid")
    else:
        if persona.looks_like_example:
            reasons.append("persona_unconfigured")
    return tuple(reasons)
