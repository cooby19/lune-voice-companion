from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lune.llm_spike.model_pin import (
    BASE_MODEL_ID,
    LOCAL_LLM_PIN,
    build_local_llm_pin,
    check_local_llm_manifest,
)
from lune.paths import LunePaths
from lune.stt.model_manifest import ModelPin, PinnedModelFile

REVISION = "b" * 40
WEIGHTS = b"pretend quantised weights"


def build_model_dir(tmp_path: Path, *, payload: bytes = WEIGHTS) -> Path:
    root = tmp_path / "qwen-local"
    root.mkdir(mode=0o700)
    weights = root / "model.safetensors"
    weights.write_bytes(payload)
    weights.chmod(0o600)
    manifest = {
        "schema_version": 1,
        "model_id": BASE_MODEL_ID,
        "revision": REVISION,
        "files": [
            {
                "relative_path": "model.safetensors",
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest_path


def matching_pin(payload: bytes = WEIGHTS) -> ModelPin:
    return build_local_llm_pin(
        model_id=BASE_MODEL_ID,
        revision=REVISION,
        files=(
            PinnedModelFile(
                relative_path="model.safetensors",
                sha256=hashlib.sha256(payload).hexdigest(),
            ),
        ),
    )


def test_pin_is_established_and_well_formed() -> None:
    assert LOCAL_LLM_PIN is not None
    assert LOCAL_LLM_PIN.model_id == BASE_MODEL_ID
    assert len(LOCAL_LLM_PIN.revision) == 40
    assert all(character in "0123456789abcdef" for character in LOCAL_LLM_PIN.revision)
    paths = [item.relative_path for item in LOCAL_LLM_PIN.files]
    assert len(paths) == len(set(paths))
    assert all(len(item.sha256) == 64 for item in LOCAL_LLM_PIN.files)


def test_pin_covers_the_chat_template() -> None:
    """The template implements `enable_thinking=False`, so it must be pinned too."""

    assert LOCAL_LLM_PIN is not None
    assert "chat_template.jinja" in {item.relative_path for item in LOCAL_LLM_PIN.files}


def test_check_fails_closed_when_no_pin_is_supplied(tmp_path: Path) -> None:
    manifest_path = build_model_dir(tmp_path)
    check = check_local_llm_manifest(manifest_path, pin=None)
    assert check.reason == "pin_not_established"
    assert not check.ready
    assert not check.pin_established


def test_check_passes_once_a_pin_matches_the_files(tmp_path: Path) -> None:
    manifest_path = build_model_dir(tmp_path)
    check = check_local_llm_manifest(manifest_path, pin=matching_pin())
    assert check.reason == "ready"
    assert check.ready
    assert check.manifest is not None
    assert check.manifest.revision == REVISION


def test_altered_weights_fail_the_checksum(tmp_path: Path) -> None:
    manifest_path = build_model_dir(tmp_path)
    (manifest_path.parent / "model.safetensors").write_bytes(b"tampered")
    check = check_local_llm_manifest(manifest_path, pin=matching_pin())
    assert check.reason == "file_checksum_mismatch"
    assert not check.ready


def test_missing_manifest_fails_closed(tmp_path: Path) -> None:
    check = check_local_llm_manifest(tmp_path / "absent.json", pin=matching_pin())
    assert check.reason == "manifest_missing"


def test_floating_revision_is_rejected(tmp_path: Path) -> None:
    manifest_path = build_model_dir(tmp_path)
    pin = build_local_llm_pin(
        model_id=BASE_MODEL_ID,
        revision="main",
        files=(
            PinnedModelFile(
                relative_path="model.safetensors",
                sha256=hashlib.sha256(WEIGHTS).hexdigest(),
            ),
        ),
    )
    check = check_local_llm_manifest(manifest_path, pin=pin)
    assert check.reason == "expected_pin_invalid"


def test_manifest_path_is_private_and_separate(tmp_path: Path) -> None:
    paths = LunePaths.defaults(tmp_path)
    assert paths.local_llm_manifest.parent.name == "qwen-local"
    assert paths.local_llm_manifest != paths.whisper_manifest
    assert paths.support in paths.local_llm_manifest.parents
