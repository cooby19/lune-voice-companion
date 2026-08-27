"""Canonical local paths for private data and privacy-safe logs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LunePaths:
    support: Path
    logs: Path

    @classmethod
    def defaults(cls, home: Path | None = None) -> LunePaths:
        base = home if home is not None else Path.home()
        return cls(
            support=base / "Library" / "Application Support" / "Lune",
            logs=base / "Library" / "Logs" / "Lune",
        )

    @property
    def config(self) -> Path:
        return self.support / "config.toml"

    @property
    def persona(self) -> Path:
        return self.support / "persona" / "kernel.yaml"

    @property
    def database(self) -> Path:
        return self.support / "lune.sqlite3"

    @property
    def whisper_manifest(self) -> Path:
        return self.support / "models" / "whisper" / "manifest.json"

    @property
    def e5_manifest(self) -> Path:
        return self.support / "models" / "e5" / "manifest.json"

    @property
    def tts_manifest(self) -> Path:
        return self.support / "voices" / "gpt-sovits" / "manifest.json"

    @property
    def gpt_sovits_runtime(self) -> Path:
        return self.support / "models" / "gpt-sovits-runtime"

    @property
    def gpt_sovits_revision(self) -> Path:
        return self.gpt_sovits_runtime / ".lune-revision"

    def ensure_private_directories(self) -> None:
        for directory in (self.support, self.persona.parent, self.logs):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
