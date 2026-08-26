"""Fail-closed validation for the private GPT-SoVITS voice manifest."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

AssetRole = Literal["gpt_checkpoint", "sovits_checkpoint", "reference_audio"]
ManifestReason = Literal[
    "ready",
    "expected_revision_invalid",
    "manifest_missing",
    "manifest_unreadable",
    "manifest_not_regular",
    "manifest_permissions_unsafe",
    "manifest_too_large",
    "manifest_invalid",
    "manifest_location_invalid",
    "manifest_revision_mismatch",
    "voice_root_invalid",
    "voice_root_permissions_unsafe",
    "runtime_revision_missing",
    "runtime_revision_invalid",
    "runtime_revision_mismatch",
    "asset_path_invalid",
    "asset_path_duplicate",
    "asset_missing",
    "asset_unreadable",
    "asset_not_regular",
    "asset_permissions_unsafe",
    "asset_checksum_mismatch",
]

_SHA256_PATTERN: Final[str] = r"^[0-9a-f]{64}$"
_REVISION_PATTERN: Final[str] = r"^[0-9a-f]{40}$"
_MAX_MANIFEST_BYTES: Final[int] = 64 * 1024
_MAX_REVISION_BYTES: Final[int] = 128


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class _AssetModel(_StrictModel):
    relative_path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=_SHA256_PATTERN, repr=False)


class _AssetsModel(_StrictModel):
    gpt_checkpoint: _AssetModel
    sovits_checkpoint: _AssetModel
    reference_audio: _AssetModel


class _ReferenceModel(_StrictModel):
    language: Literal["zh", "en", "auto"]
    prompt_text: str = Field(min_length=1, max_length=2_000, repr=False)


class _ManifestModel(_StrictModel):
    schema_version: Literal[1]
    upstream_commit: str = Field(pattern=_REVISION_PATTERN)
    assets: _AssetsModel
    reference: _ReferenceModel


@dataclass(frozen=True, slots=True)
class VerifiedAsset:
    role: AssetRole
    path: Path = field(repr=False)
    sha256: str = field(repr=False)
    size_bytes: int


@dataclass(frozen=True, slots=True)
class VerifiedPrivateManifest:
    upstream_commit: str = field(repr=False)
    assets: tuple[VerifiedAsset, ...] = field(repr=False)
    reference_language: str = field(repr=False)
    reference_text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class PrivateManifestCheck:
    reason: ManifestReason
    manifest: VerifiedPrivateManifest | None = field(default=None, repr=False)

    @property
    def ready(self) -> bool:
        return self.reason == "ready" and self.manifest is not None


def _unavailable(reason: ManifestReason) -> PrivateManifestCheck:
    return PrivateManifestCheck(reason=reason)


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


def _validate_voice_root(path: Path) -> ManifestReason | None:
    try:
        metadata = path.lstat()
    except OSError:
        return "voice_root_invalid"
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        return "voice_root_invalid"
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        return "voice_root_permissions_unsafe"
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


def _validate_asset_directories(root: Path, parts: tuple[str, ...]) -> ManifestReason | None:
    current = root
    for part in parts[:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError:
            return "asset_missing"
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            return "asset_path_invalid"
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            return "asset_permissions_unsafe"
    return None


def _verify_asset(
    *, role: AssetRole, root: Path, model: _AssetModel
) -> tuple[VerifiedAsset | None, ManifestReason | None]:
    parts = _validate_relative_path(model.relative_path)
    if parts is None:
        return None, "asset_path_invalid"
    directory_reason = _validate_asset_directories(root, parts)
    if directory_reason is not None:
        return None, directory_reason

    path = root.joinpath(*parts)
    try:
        file_descriptor = _secure_open(path)
    except FileNotFoundError:
        return None, "asset_missing"
    except OSError:
        return None, "asset_unreadable"

    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None, "asset_not_regular"
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            return None, "asset_permissions_unsafe"
        digest = hashlib.sha256()
        with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != model.sha256:
            return None, "asset_checksum_mismatch"
        return (
            VerifiedAsset(
                role=role,
                path=path,
                sha256=model.sha256,
                size_bytes=metadata.st_size,
            ),
            None,
        )
    finally:
        os.close(file_descriptor)


def _read_runtime_revision(path: Path) -> tuple[str | None, ManifestReason | None]:
    try:
        file_descriptor = _secure_open(path)
    except FileNotFoundError:
        return None, "runtime_revision_missing"
    except OSError:
        return None, "runtime_revision_invalid"

    try:
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            return None, "runtime_revision_invalid"
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            return None, "runtime_revision_invalid"
        if metadata.st_size > _MAX_REVISION_BYTES:
            return None, "runtime_revision_invalid"
        with os.fdopen(file_descriptor, "rb", closefd=False) as handle:
            try:
                value = handle.read(_MAX_REVISION_BYTES + 1).decode("ascii").strip()
            except UnicodeDecodeError:
                return None, "runtime_revision_invalid"
    finally:
        os.close(file_descriptor)

    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        return None, "runtime_revision_invalid"
    return value, None


def check_private_manifest(
    *,
    manifest_path: Path,
    voice_root: Path,
    runtime_revision_path: Path,
    expected_upstream_commit: str,
) -> PrivateManifestCheck:
    """Validate private assets without putting their values into result strings or reprs."""

    if len(expected_upstream_commit) != 40 or any(
        character not in "0123456789abcdef" for character in expected_upstream_commit
    ):
        return _unavailable("expected_revision_invalid")

    raw_manifest, read_reason = _read_manifest(manifest_path)
    if read_reason is not None or raw_manifest is None:
        return _unavailable(read_reason or "manifest_unreadable")

    root_reason = _validate_voice_root(voice_root)
    if root_reason is not None:
        return _unavailable(root_reason)
    try:
        if manifest_path.parent.resolve(strict=True) != voice_root.resolve(strict=True):
            return _unavailable("manifest_location_invalid")
    except OSError:
        return _unavailable("manifest_location_invalid")

    try:
        model = _ManifestModel.model_validate_json(raw_manifest)
    except ValidationError:
        return _unavailable("manifest_invalid")
    if model.upstream_commit != expected_upstream_commit:
        return _unavailable("manifest_revision_mismatch")

    runtime_revision, runtime_reason = _read_runtime_revision(runtime_revision_path)
    if runtime_reason is not None or runtime_revision is None:
        return _unavailable(runtime_reason or "runtime_revision_invalid")
    if runtime_revision != expected_upstream_commit:
        return _unavailable("runtime_revision_mismatch")

    asset_models: tuple[tuple[AssetRole, _AssetModel], ...] = (
        ("gpt_checkpoint", model.assets.gpt_checkpoint),
        ("sovits_checkpoint", model.assets.sovits_checkpoint),
        ("reference_audio", model.assets.reference_audio),
    )
    relative_paths = [asset.relative_path for _, asset in asset_models]
    if len(set(relative_paths)) != len(relative_paths):
        return _unavailable("asset_path_duplicate")

    verified_assets: list[VerifiedAsset] = []
    for role, asset_model in asset_models:
        asset, asset_reason = _verify_asset(role=role, root=voice_root, model=asset_model)
        if asset_reason is not None or asset is None:
            return _unavailable(asset_reason or "asset_unreadable")
        verified_assets.append(asset)

    return PrivateManifestCheck(
        reason="ready",
        manifest=VerifiedPrivateManifest(
            upstream_commit=model.upstream_commit,
            assets=tuple(verified_assets),
            reference_language=model.reference.language,
            reference_text=model.reference.prompt_text,
        ),
    )
