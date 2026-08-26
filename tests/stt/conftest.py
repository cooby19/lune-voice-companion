from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from lune.stt.model_manifest import ModelPin, PinnedModelFile

TEST_MODEL_ID = "test/whisper-model"
TEST_REVISION = "a" * 40


@dataclass(frozen=True, slots=True)
class ModelFixture:
    root: Path
    manifest_path: Path
    file_paths: tuple[Path, ...]
    pin: ModelPin

    def rewrite_manifest(self, payload: dict[str, object]) -> None:
        self.manifest_path.write_text(json.dumps(payload), encoding="utf-8")
        self.manifest_path.chmod(0o600)

    def read_manifest(self) -> dict[str, object]:
        raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        return raw


@pytest.fixture
def model_fixture(tmp_path: Path) -> ModelFixture:
    root = tmp_path / "whisper"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    contents = {
        "config.json": b'{"model_type":"whisper"}\n',
        "weights.npz": b"deterministic fake weights",
    }
    file_paths: list[Path] = []
    pinned_files: list[PinnedModelFile] = []
    manifest_files: list[dict[str, str]] = []
    for relative_path, content in contents.items():
        path = root / relative_path
        path.write_bytes(content)
        path.chmod(0o600)
        digest = hashlib.sha256(content).hexdigest()
        file_paths.append(path)
        pinned_files.append(PinnedModelFile(relative_path=relative_path, sha256=digest))
        manifest_files.append({"relative_path": relative_path, "sha256": digest})

    manifest_path = root / "manifest.json"
    fixture = ModelFixture(
        root=root,
        manifest_path=manifest_path,
        file_paths=tuple(file_paths),
        pin=ModelPin(
            model_id=TEST_MODEL_ID,
            revision=TEST_REVISION,
            files=tuple(pinned_files),
        ),
    )
    fixture.rewrite_manifest(
        {
            "schema_version": 1,
            "model_id": TEST_MODEL_ID,
            "revision": TEST_REVISION,
            "files": manifest_files,
        }
    )
    return fixture
