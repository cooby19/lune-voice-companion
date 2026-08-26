from __future__ import annotations

from pathlib import Path

from lune.stt.model_manifest import check_model_manifest

from .conftest import ModelFixture


def test_valid_manifest_verifies_every_file_without_repr_leaks(
    model_fixture: ModelFixture,
) -> None:
    check = check_model_manifest(model_fixture.manifest_path, pin=model_fixture.pin)
    assert check.ready
    assert check.reason == "ready"
    assert check.manifest is not None
    assert len(check.manifest.files) == 2
    rendered = repr(check)
    assert str(model_fixture.root) not in rendered
    assert model_fixture.pin.revision not in rendered
    assert model_fixture.pin.files[0].sha256 not in rendered


def test_missing_file_fails_closed(model_fixture: ModelFixture) -> None:
    missing = model_fixture.root / "missing.json"
    check = check_model_manifest(missing, pin=model_fixture.pin)
    assert not check.ready
    assert check.reason == "manifest_missing"


def test_missing_pinned_model_file_fails_closed(model_fixture: ModelFixture) -> None:
    model_fixture.file_paths[1].unlink()
    check = check_model_manifest(model_fixture.manifest_path, pin=model_fixture.pin)
    assert not check.ready
    assert check.reason == "file_missing"


def test_wrong_hash_fails_closed(model_fixture: ModelFixture) -> None:
    model_fixture.file_paths[0].write_bytes(b"changed")
    model_fixture.file_paths[0].chmod(0o600)
    check = check_model_manifest(model_fixture.manifest_path, pin=model_fixture.pin)
    assert not check.ready
    assert check.reason == "file_checksum_mismatch"


def test_floating_revision_is_rejected(model_fixture: ModelFixture) -> None:
    payload = model_fixture.read_manifest()
    payload["revision"] = "main"
    model_fixture.rewrite_manifest(payload)
    check = check_model_manifest(model_fixture.manifest_path, pin=model_fixture.pin)
    assert not check.ready
    assert check.reason == "manifest_revision_invalid"


def test_parent_escape_is_rejected_before_pin_comparison(model_fixture: ModelFixture) -> None:
    payload = model_fixture.read_manifest()
    files = payload["files"]
    assert isinstance(files, list)
    first = files[0]
    assert isinstance(first, dict)
    first["relative_path"] = "../config.json"
    model_fixture.rewrite_manifest(payload)
    check = check_model_manifest(model_fixture.manifest_path, pin=model_fixture.pin)
    assert not check.ready
    assert check.reason == "file_path_invalid"


def test_symlink_is_rejected(model_fixture: ModelFixture, tmp_path: Path) -> None:
    outside = tmp_path / "outside.npz"
    outside.write_bytes(b"deterministic fake weights")
    outside.chmod(0o600)
    model_fixture.file_paths[1].unlink()
    model_fixture.file_paths[1].symlink_to(outside)
    check = check_model_manifest(model_fixture.manifest_path, pin=model_fixture.pin)
    assert not check.ready
    assert check.reason == "file_path_invalid"


def test_non_regular_file_is_rejected(model_fixture: ModelFixture) -> None:
    model_fixture.file_paths[1].unlink()
    model_fixture.file_paths[1].mkdir(mode=0o700)
    check = check_model_manifest(model_fixture.manifest_path, pin=model_fixture.pin)
    assert not check.ready
    assert check.reason == "file_not_regular"


def test_manifest_file_list_must_match_the_compiled_pin(model_fixture: ModelFixture) -> None:
    payload = model_fixture.read_manifest()
    files = payload["files"]
    assert isinstance(files, list)
    payload["files"] = files[:1]
    model_fixture.rewrite_manifest(payload)
    check = check_model_manifest(model_fixture.manifest_path, pin=model_fixture.pin)
    assert not check.ready
    assert check.reason == "file_set_mismatch"
