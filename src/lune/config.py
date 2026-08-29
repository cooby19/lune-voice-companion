"""Strict public configuration and private persona loading."""

from __future__ import annotations

import os
import stat
import tempfile
import tomllib
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelsConfig(StrictModel):
    # The test phase runs on-device only: no API key, no network, no spend. The
    # cloud pair below stays configured so restoring it is a one-line change once
    # `docs/ui-spec.md`'s hybrid composition is ready.
    provider: Literal["local_qwen", "openai_responses"] = "local_qwen"
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

    def save(self, path: Path) -> None:
        """Write the complete, non-secret configuration with private permissions."""

        _write_private_text(path, _config_toml(self))


def ensure_default_config(path: Path) -> bool:
    """Create the all-default configuration only when it does not yet exist.

    A malformed file is evidence the user needs to resolve, not permission for
    the application to replace it.  Returning whether a file was created lets
    the onboarding layer refresh its reason codes without exposing file data.
    """

    if path.exists() or path.is_symlink():
        return False
    AppConfig().save(path)
    return True


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

    def save(self, path: Path) -> None:
        """Persist only the structured, validated persona fields exposed by the UI."""

        contents = yaml.safe_dump(
            self.model_dump(mode="json"),
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        _write_private_text(path, contents)

    @property
    def looks_like_example(self) -> bool:
        return self.identity.user_address.strip().casefold() == "friend"


class UserProfile(StrictModel):
    """The user's explicit, always-in-context profile — distinct from memory."""

    name: str = Field(default="", max_length=80)
    context: str = Field(default="", max_length=8_000)

    @classmethod
    def load(cls, path: Path) -> UserProfile:
        with path.open("rb") as handle:
            return cls.model_validate(tomllib.load(handle))

    def save(self, path: Path) -> None:
        _write_private_text(path, _profile_toml(self))


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


def _config_toml(config: AppConfig) -> str:
    """Serialize the fixed, validated schema without a general TOML writer."""

    return "\n".join(
        (
            f"schema_version = {config.schema_version}",
            "",
            "[models]",
            f'provider = "{config.models.provider}"',
            f'primary = "{config.models.primary}"',
            f'fallback = "{config.models.fallback}"',
            f'reasoning_effort = "{config.models.reasoning_effort}"',
            f"max_output_tokens = {config.models.max_output_tokens}",
            "",
            "[audio]",
            f"sample_rate = {config.audio.sample_rate}",
            f"channels = {config.audio.channels}",
            f"turn_start_ms = {config.audio.turn_start_ms}",
            f"barge_in_ms = {config.audio.barge_in_ms}",
            f"pre_roll_ms = {config.audio.pre_roll_ms}",
            f"end_silence_ms = {config.audio.end_silence_ms}",
            "",
            "[budget]",
            f'timezone = "{config.budget.timezone}"',
            f"twd_per_usd = {config.budget.twd_per_usd}",
            f"fallback_at_twd = {config.budget.fallback_at_twd}",
            f"lock_at_twd = {config.budget.lock_at_twd}",
            "",
            "[privacy]",
            f"store_openai_responses = {_toml_bool(config.privacy.store_openai_responses)}",
            f"telemetry = {_toml_bool(config.privacy.telemetry)}",
            f"tracing = {_toml_bool(config.privacy.tracing)}",
            "",
            "[tts]",
            f'preferred_backend = "{config.tts.preferred_backend}"',
            f'gpt_worker_python = "{config.tts.gpt_worker_python}"',
            "",
        )
    )


def _profile_toml(profile: UserProfile) -> str:
    return f"name = {_toml_string(profile.name)}\ncontext = {_toml_string(profile.context)}\n"


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_string(value: str) -> str:
    """Quote a string without allowing it to escape its TOML value."""

    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _write_private_text(path: Path, contents: str) -> None:
    """Atomically write a local private file without following a symlink."""

    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = parent.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("private configuration directory must be a real directory")
    parent.chmod(0o700)
    if path.is_symlink():
        raise ValueError("private configuration path must not be a symlink")
    if path.exists() and not path.is_file():
        raise ValueError("private configuration path must be a regular file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".lune-", dir=parent, text=True)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        if temporary.exists():
            temporary.unlink()
        raise
