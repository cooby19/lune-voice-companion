"""Privacy-safe JSON-lines diagnostics with a strict field allowlist."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Final

ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "event",
        "state",
        "previous_state",
        "error_code",
        "duration_ms",
        "generation_id",
        "queue_depth",
        "rss_bytes",
        "thermal_state",
        "backend",
        "device_class",
        "count",
    }
)
SENSITIVE_FRAGMENTS: Final[tuple[str, ...]] = (
    "text",
    "transcript",
    "prompt",
    "persona",
    "memory",
    "key",
    "token",
    "path",
    "audio",
    "pcm",
)


class UnsafeDiagnosticField(ValueError):
    pass


def _validate_fields(fields: Mapping[str, object]) -> None:
    for field in fields:
        lowered = field.casefold()
        if field not in ALLOWED_FIELDS or any(part in lowered for part in SENSITIVE_FRAGMENTS):
            raise UnsafeDiagnosticField(f"diagnostic field is not allowlisted: {field}")


class SafeDiagnostics:
    def __init__(self, log_path: Path) -> None:
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log_path.parent.chmod(0o700)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger = logging.getLogger(f"lune.safe.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._logger.addHandler(handler)

    def emit(self, **fields: object) -> None:
        _validate_fields(fields)
        self._logger.info(json.dumps(fields, ensure_ascii=True, separators=(",", ":")))
