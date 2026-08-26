from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

PINNED_TEST_COMMIT = "a" * 40
PRIVATE_TEST_PHRASE = "private fixture phrase that must never enter a report"


@dataclass(frozen=True, slots=True)
class PrivateVoiceFixture:
    voice_root: Path
    manifest_path: Path
    runtime_revision_path: Path
    asset_paths: tuple[Path, ...]


def _write_private_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_bytes(content)
    path.chmod(0o600)


@pytest.fixture
def private_voice(tmp_path: Path) -> PrivateVoiceFixture:
    voice_root = tmp_path / "private-voice"
    voice_root.mkdir(mode=0o700)
    semantic = voice_root / "models" / "semantic.ckpt"
    acoustic = voice_root / "models" / "acoustic.pth"
    reference = voice_root / "reference.wav"
    _write_private_file(semantic, b"semantic fixture")
    _write_private_file(acoustic, b"acoustic fixture")
    _write_private_file(reference, b"reference fixture")

    def asset(path: Path) -> dict[str, str]:
        return {
            "relative_path": path.relative_to(voice_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    manifest = {
        "schema_version": 1,
        "upstream_commit": PINNED_TEST_COMMIT,
        "assets": {
            "gpt_checkpoint": asset(semantic),
            "sovits_checkpoint": asset(acoustic),
            "reference_audio": asset(reference),
        },
        "reference": {"language": "zh", "prompt_text": PRIVATE_TEST_PHRASE},
    }
    manifest_path = voice_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)

    runtime_revision_path = tmp_path / "runtime" / "REVISION"
    runtime_revision_path.parent.mkdir(mode=0o700)
    runtime_revision_path.write_text(f"{PINNED_TEST_COMMIT}\n", encoding="ascii")
    runtime_revision_path.chmod(0o644)
    return PrivateVoiceFixture(
        voice_root=voice_root,
        manifest_path=manifest_path,
        runtime_revision_path=runtime_revision_path,
        asset_paths=(semantic, acoustic, reference),
    )
