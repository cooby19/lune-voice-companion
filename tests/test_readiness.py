from __future__ import annotations

from pathlib import Path

import keyring.errors
import pytest

from lune.paths import LunePaths
from lune.readiness import check_readiness


def test_keychain_failure_is_setup_required(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = LunePaths(support=tmp_path / "support", logs=tmp_path / "logs")

    def fail() -> str | None:
        raise keyring.errors.KeyringError("locked")

    monkeypatch.setattr("lune.readiness.get_openai_api_key", fail)
    readiness = check_readiness(paths)
    assert readiness.state == "setup_required"
    assert "keychain_unavailable" in readiness.reasons
