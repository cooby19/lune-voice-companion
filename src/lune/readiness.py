"""Compute public application state without exposing private values."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from typing import Literal

import keyring.errors
from pydantic import ValidationError

from lune.config import AppConfig, ModelsConfig, ensure_default_config, validate_private_setup
from lune.keychain import get_openai_api_key
from lune.paths import LunePaths

AppState = Literal[
    "setup_required",
    "mic_off",
    "listening",
    "thinking",
    "speaking",
    "paused_unsafe_output",
    "degraded_tts",
    "degraded_llm",
    "budget_locked",
    "error",
]


@dataclass(frozen=True, slots=True)
class Readiness:
    state: AppState
    reasons: tuple[str, ...]


def check_readiness(paths: LunePaths) -> Readiness:
    # Every config field has a safe default.  A missing file is first-run
    # housekeeping, not a setup task; invalid existing files remain visible.
    try:
        ensure_default_config(paths.config)
    except (OSError, ValueError):
        pass
    reasons = list(validate_private_setup(paths.config, paths.persona))
    if _configured_provider(paths) == "openai_responses":
        try:
            api_key = get_openai_api_key()
        except keyring.errors.KeyringError:
            reasons.append("keychain_unavailable")
        else:
            if not api_key:
                reasons.append("api_key_missing")
    else:
        # The on-device composition needs no key at all; it needs the pinned
        # artifact and the worker runtime that loads it.
        if not paths.local_llm_manifest.is_file():
            reasons.append("local_llm_model_missing")
        if not paths.local_llm_runtime_python.is_file():
            reasons.append("local_llm_runtime_missing")
    if not paths.whisper_manifest.is_file():
        reasons.append("whisper_model_missing")
    if not paths.e5_manifest.is_file():
        reasons.append("embedding_model_missing")
    if reasons:
        return Readiness(state="setup_required", reasons=tuple(reasons))
    return Readiness(state="mic_off", reasons=())


def _configured_provider(paths: LunePaths) -> str:
    """Fall back to the declared default; an unreadable config is already a reason."""

    try:
        return AppConfig.load(paths.config).models.provider
    except (OSError, tomllib.TOMLDecodeError, ValidationError):
        return ModelsConfig().provider
