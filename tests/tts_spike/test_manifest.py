from __future__ import annotations

import json

from lune.tts_spike.manifest import PrivateManifestCheck, check_private_manifest

from .conftest import PINNED_TEST_COMMIT, PRIVATE_TEST_PHRASE, PrivateVoiceFixture


def _check(private_voice: PrivateVoiceFixture) -> PrivateManifestCheck:
    return check_private_manifest(
        manifest_path=private_voice.manifest_path,
        voice_root=private_voice.voice_root,
        runtime_revision_path=private_voice.runtime_revision_path,
        expected_upstream_commit=PINNED_TEST_COMMIT,
    )


def test_private_manifest_validates_without_repr_leaks(
    private_voice: PrivateVoiceFixture,
) -> None:
    check = _check(private_voice)
    assert check.ready
    assert check.reason == "ready"
    rendered = repr(check)
    assert PRIVATE_TEST_PHRASE not in rendered
    assert str(private_voice.voice_root) not in rendered
    assert PINNED_TEST_COMMIT not in rendered


def test_missing_manifest_fails_closed(private_voice: PrivateVoiceFixture) -> None:
    missing = private_voice.voice_root / "missing.json"
    check = check_private_manifest(
        manifest_path=missing,
        voice_root=private_voice.voice_root,
        runtime_revision_path=private_voice.runtime_revision_path,
        expected_upstream_commit=PINNED_TEST_COMMIT,
    )
    assert not check.ready
    assert check.reason == "manifest_missing"


def test_checksum_mismatch_fails_closed(private_voice: PrivateVoiceFixture) -> None:
    private_voice.asset_paths[0].write_bytes(b"changed after manifest creation")
    private_voice.asset_paths[0].chmod(0o600)
    check = _check(private_voice)
    assert not check.ready
    assert check.reason == "asset_checksum_mismatch"


def test_relative_path_escape_is_rejected(private_voice: PrivateVoiceFixture) -> None:
    raw = json.loads(private_voice.manifest_path.read_text(encoding="utf-8"))
    raw["assets"]["reference_audio"]["relative_path"] = "../outside.wav"
    private_voice.manifest_path.write_text(json.dumps(raw), encoding="utf-8")
    private_voice.manifest_path.chmod(0o600)
    check = _check(private_voice)
    assert not check.ready
    assert check.reason == "asset_path_invalid"


def test_private_asset_permissions_are_enforced(private_voice: PrivateVoiceFixture) -> None:
    private_voice.asset_paths[-1].chmod(0o644)
    check = _check(private_voice)
    assert not check.ready
    assert check.reason == "asset_permissions_unsafe"


def test_runtime_revision_must_match_pin(private_voice: PrivateVoiceFixture) -> None:
    private_voice.runtime_revision_path.write_text(f"{'b' * 40}\n", encoding="ascii")
    check = _check(private_voice)
    assert not check.ready
    assert check.reason == "runtime_revision_mismatch"
