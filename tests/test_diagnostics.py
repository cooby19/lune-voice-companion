from __future__ import annotations

from pathlib import Path

import pytest

from lune.diagnostics import SafeDiagnostics, UnsafeDiagnosticField


def test_diagnostics_accept_only_opaque_fields(tmp_path: Path) -> None:
    log = tmp_path / "lune.log"
    diagnostics = SafeDiagnostics(log)
    diagnostics.emit(event="state", state="mic_off", generation_id=1)
    text = log.read_text(encoding="utf-8")
    assert "mic_off" in text


@pytest.mark.parametrize("field", ["transcript", "prompt", "api_key", "voice_path", "pcm"])
def test_diagnostics_reject_sensitive_fields(tmp_path: Path, field: str) -> None:
    diagnostics = SafeDiagnostics(tmp_path / "lune.log")
    with pytest.raises(UnsafeDiagnosticField):
        diagnostics.emit(**{field: "private"})
