"""Compute public application state without exposing private values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import keyring.errors

from lune.config import validate_private_setup
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
    "budget_locked",
    "error",
]


@dataclass(frozen=True, slots=True)
class Readiness:
    state: AppState
    reasons: tuple[str, ...]


def check_readiness(paths: LunePaths) -> Readiness:
    reasons = list(validate_private_setup(paths.config, paths.persona))
    try:
        api_key = get_openai_api_key()
    except keyring.errors.KeyringError:
        reasons.append("keychain_unavailable")
    else:
        if not api_key:
            reasons.append("api_key_missing")
    if not paths.whisper_manifest.is_file():
        reasons.append("whisper_model_missing")
    if not paths.e5_manifest.is_file():
        reasons.append("embedding_model_missing")
    if reasons:
        return Readiness(state="setup_required", reasons=tuple(reasons))
    return Readiness(state="mic_off", reasons=())
