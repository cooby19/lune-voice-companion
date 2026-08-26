"""macOS Keychain access. Keys are never accepted as CLI arguments."""

from __future__ import annotations

import keyring

SERVICE = "dev.lune.voice-companion"
ACCOUNT = "openai-api-key"


def get_openai_api_key() -> str | None:
    value = keyring.get_password(SERVICE, ACCOUNT)
    return value if value else None


def set_openai_api_key(value: str) -> None:
    candidate = value.strip()
    if not candidate:
        raise ValueError("API key cannot be empty")
    keyring.set_password(SERVICE, ACCOUNT, candidate)


def delete_openai_api_key() -> None:
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except keyring.errors.PasswordDeleteError:
        return
