"""Fail-closed verification for the pinned local MLX Whisper model."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

WHISPER_MODEL_ID: Final[str] = "mlx-community/whisper-large-v3-turbo-q4"
WHISPER_MODEL_REVISION: Final[str] = "660c343bbf4e52ac257f0b7d952e5388e6f93bef"

type ManifestReason = Literal[
    "ready",
    "expected_pin_invalid",
    "manifest_missing",
    "manifest_unreadable",
    "manifest_not_regular",
    "manifest_permissions_unsafe",
    "manifest_too_large",
    "manifest_invalid",
    "model_root_invalid",
    "model_root_permissions_unsafe",
    "model_id_mismatch",
    "manifest_revision_invalid",
    "manifest_revision_mismatch",
    "file_path_invalid",
    "file_path_duplicate",
    "file_set_mismatch",
    "file_missing",
    "file_unreadable",
    "file_not_regular",
    "file_permissions_unsafe",
    "file_checksum_mismatch",
]

_SHA256_PATTERN: Final[str] = r"^[0-9a-f]{64}$"
_MAX_MANIFEST_BYTES: Final[int] = 64 * 1024


@dataclass(frozen=True, slots=True)
class PinnedModelFile:
    relative_path: str
    sha256: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelPin:
    model_id: str
    revision: str = field(repr=False)
    files: tuple[PinnedModelFile, ...] = field(repr=False)


WHISPER_MODEL_PIN: Final[ModelPin] = ModelPin(
    model_id=WHISPER_MODEL_ID,
    revision=WHISPER_MODEL_REVISION,
    files=(
        PinnedModelFile(
            relative_path="config.json",
            sha256="538e24557b8f9bc504700add5e7bbe32087c2353001ff563e64772ad4398671a",
        ),
        PinnedModelFile(
            relative_path="weights.npz",
            sha256="862bbc832b05f3f4ec19dd632b701d61a6d3f5c7906360a10d72a79870642a80",
        ),
    ),
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _FileModel(_StrictModel):
    relative_path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=_SHA256_PATTERN, repr=False)


class _ManifestModel(_StrictModel):
    schema_version: Literal[1]
    model_id: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=128, repr=False)
    files: tuple[_FileModel, ...] = Field(min_length=1, max_length=32, repr=False)


@dataclass(frozen=True, slots=True)
class VerifiedModelFile:
    path: Path = field(repr=False)
    sha256: str = field(repr=False)
    size_bytes: int


@dataclass(frozen=True, slots=True)
class VerifiedModelManifest:
    model_id: str
    revision: str = field(repr=False)
    model_root: Path = field(repr=False)
    files: tuple[VerifiedModelFile, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelManifestCheck:
    reason: ManifestReason
    manifest: VerifiedModelManifest | None = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return self.reason == "ready" and self.manifest is not None


def _unavailable(reason: ManifestReason) -> ModelManifestCheck:
    return ModelManifestCheck(reason=reason)


def _secure_open(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _read_manifest(path: Path) -> tuple[bytes | None, ManifestReason | None]:
    try:
        file_descriptor = _secure_open(path)
    except FileNotFoundError:
        return None, "manifest_missing"
    except OSError:
        return None, "manifest_unreadable"

    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None, "manifest_not_regular"
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            return None, "manifest_permissions_unsafe"
        if metadata.st_size > _MAX_MANIFEST_BYTES:
            return None, "manifest_too_large"
        with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
            return handle.read(_MAX_MANIFEST_BYTES + 1), None
    finally:
        os.close(file_descriptor)


def _validate_model_root(path: Path) -> ManifestReason | None:
    try:
        metadata = path.lstat()
    except OSError:
        return "model_root_invalid"
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return "model_root_invalid"
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        return "model_root_permissions_unsafe"
    return None


def _validate_relative_path(relative_path: str) -> tuple[str, ...] | None:
    if "\\" in relative_path or "\x00" in relative_path:
        return None
    raw_parts = relative_path.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        return None
    candidate = PurePosixPath(relative_path)
    if candidate.is_absolute():
        return None
    return tuple(candidate.parts)


def _validate_parent_directories(root: Path, parts: tuple[str, ...]) -> ManifestReason | None:
    current = root
    for part in parts[:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            return "file_missing"
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return "file_path_invalid"
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            return "file_permissions_unsafe"
    return None


def _verify_file(
    root: Path, file_model: _FileModel
) -> tuple[VerifiedModelFile | None, ManifestReason | None]:
    parts = _validate_relative_path(file_model.relative_path)
    if parts is None:
        return None, "file_path_invalid"
    parent_reason = _validate_parent_directories(root, parts)
    if parent_reason is not None:
        return None, parent_reason

    path = root.joinpath(*parts)
    try:
        path_metadata = path.lstat()
    except FileNotFoundError:
        return None, "file_missing"
    except OSError:
        return None, "file_unreadable"
    if stat.S_ISLNK(path_metadata.st_mode):
        return None, "file_path_invalid"

    try:
        file_descriptor = _secure_open(path)
    except FileNotFoundError:
        return None, "file_missing"
    except OSError:
        return None, "file_unreadable"

    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None, "file_not_regular"
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            return None, "file_permissions_unsafe"
        digest = hashlib.sha256()
        with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != file_model.sha256:
            return None, "file_checksum_mismatch"
        return (
            VerifiedModelFile(
                path=path,
                sha256=file_model.sha256,
                size_bytes=metadata.st_size,
            ),
            None,
        )
    finally:
        os.close(file_descriptor)


def _pin_is_valid(pin: ModelPin) -> bool:
    if not pin.model_id or len(pin.revision) != 40:
        return False
    if any(character not in "0123456789abcdef" for character in pin.revision):
        return False
    paths = [item.relative_path for item in pin.files]
    if not paths or len(paths) != len(set(paths)):
        return False
    return all(
        _validate_relative_path(item.relative_path) is not None
        and len(item.sha256) == 64
        and all(character in "0123456789abcdef" for character in item.sha256)
        for item in pin.files
    )


def check_model_manifest(
    manifest_path: Path,
    *,
    pin: ModelPin = WHISPER_MODEL_PIN,
) -> ModelManifestCheck:
    """Verify the manifest and every pinned file without returning private values in reprs."""

    if not _pin_is_valid(pin):
        return _unavailable("expected_pin_invalid")

    raw_manifest, read_reason = _read_manifest(manifest_path)
    if read_reason is not None or raw_manifest is None:
        return _unavailable(read_reason or "manifest_unreadable")

    model_root = manifest_path.parent
    root_reason = _validate_model_root(model_root)
    if root_reason is not None:
        return _unavailable(root_reason)
    try:
        model = _ManifestModel.model_validate_json(raw_manifest)
    except ValidationError:
        return _unavailable("manifest_invalid")
    if model.model_id != pin.model_id:
        return _unavailable("model_id_mismatch")
    if len(model.revision) != 40 or any(
        character not in "0123456789abcdef" for character in model.revision
    ):
        return _unavailable("manifest_revision_invalid")
    if model.revision != pin.revision:
        return _unavailable("manifest_revision_mismatch")

    relative_paths: list[str] = []
    for item in model.files:
        if _validate_relative_path(item.relative_path) is None:
            return _unavailable("file_path_invalid")
        relative_paths.append(item.relative_path)
    if len(relative_paths) != len(set(relative_paths)):
        return _unavailable("file_path_duplicate")
    expected = {(item.relative_path, item.sha256) for item in pin.files}
    provided = {(item.relative_path, item.sha256) for item in model.files}
    if provided != expected:
        return _unavailable("file_set_mismatch")

    verified_files: list[VerifiedModelFile] = []
    for file_model in model.files:
        verified_file, file_reason = _verify_file(model_root, file_model)
        if file_reason is not None or verified_file is None:
            return _unavailable(file_reason or "file_unreadable")
        verified_files.append(verified_file)

    return ModelManifestCheck(
        reason="ready",
        manifest=VerifiedModelManifest(
            model_id=model.model_id,
            revision=model.revision,
            model_root=model_root,
            files=tuple(verified_files),
        ),
    )
