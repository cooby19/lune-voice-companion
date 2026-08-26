from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from lune.tts_spike.evaluation import evaluate_spike
from lune.tts_spike.performance import SpikeMeasurements
from lune.tts_spike.report import write_sanitized_report
from lune.tts_spike.sandbox import SandboxCheck

from .conftest import PINNED_TEST_COMMIT, PRIVATE_TEST_PHRASE, PrivateVoiceFixture


def passing_measurements() -> SpikeMeasurements:
    return SpikeMeasurements(
        zh_samples=1,
        en_samples=1,
        mixed_samples=1,
        ttfa_ms=(500.0, 700.0, 900.0),
        rtf=(0.4, 0.6, 0.8),
        peak_rss_bytes=5 * 1024**3,
        thermal_states=("nominal", "fair"),
        cancel_stop_ms=(120.0, 499.0),
    )


def test_missing_assets_force_avspeech_without_probing(tmp_path: Path) -> None:
    probed = False

    def probe(_canary: Path) -> SandboxCheck:
        nonlocal probed
        probed = True
        return SandboxCheck(reason="available", executable=Path("/usr/bin/sandbox-exec"))

    voice_root = tmp_path / "voice"
    voice_root.mkdir(mode=0o700)
    evaluation = evaluate_spike(
        manifest_path=voice_root / "manifest.json",
        voice_root=voice_root,
        runtime_revision_path=tmp_path / "runtime" / "REVISION",
        expected_upstream_commit=PINNED_TEST_COMMIT,
        measurements=passing_measurements(),
        sandbox_probe=probe,
    )
    assert not probed
    assert evaluation.decision.default_backend == "avspeech"
    assert not evaluation.decision.gpt_sovits_enabled
    assert evaluation.report.manifest_status == "manifest_missing"
    assert evaluation.report.sandbox_status == "not_probed"


def test_all_m05_gates_must_pass_for_gpt_default(private_voice: PrivateVoiceFixture) -> None:
    evaluation = evaluate_spike(
        manifest_path=private_voice.manifest_path,
        voice_root=private_voice.voice_root,
        runtime_revision_path=private_voice.runtime_revision_path,
        expected_upstream_commit=PINNED_TEST_COMMIT,
        measurements=passing_measurements(),
        sandbox_probe=lambda _canary: SandboxCheck(
            reason="available", executable=Path("/usr/bin/sandbox-exec")
        ),
    )
    assert evaluation.performance_gate.passed
    assert evaluation.decision.default_backend == "gpt_sovits"
    assert evaluation.decision.gpt_sovits_enabled


def test_cancel_over_500ms_forces_avspeech(private_voice: PrivateVoiceFixture) -> None:
    measurements = passing_measurements()
    slow_cancel = SpikeMeasurements(
        zh_samples=measurements.zh_samples,
        en_samples=measurements.en_samples,
        mixed_samples=measurements.mixed_samples,
        ttfa_ms=measurements.ttfa_ms,
        rtf=measurements.rtf,
        peak_rss_bytes=measurements.peak_rss_bytes,
        thermal_states=measurements.thermal_states,
        cancel_stop_ms=(500.1,),
    )
    evaluation = evaluate_spike(
        manifest_path=private_voice.manifest_path,
        voice_root=private_voice.voice_root,
        runtime_revision_path=private_voice.runtime_revision_path,
        expected_upstream_commit=PINNED_TEST_COMMIT,
        measurements=slow_cancel,
        sandbox_probe=lambda _canary: SandboxCheck(
            reason="available", executable=Path("/usr/bin/sandbox-exec")
        ),
    )
    assert not evaluation.performance_gate.passed
    assert "cancel_deadline_exceeded" in evaluation.performance_gate.reasons
    assert evaluation.decision.default_backend == "avspeech"


def test_report_contains_no_private_manifest_values(private_voice: PrivateVoiceFixture) -> None:
    evaluation = evaluate_spike(
        manifest_path=private_voice.manifest_path,
        voice_root=private_voice.voice_root,
        runtime_revision_path=private_voice.runtime_revision_path,
        expected_upstream_commit=PINNED_TEST_COMMIT,
        measurements=passing_measurements(),
        sandbox_probe=lambda _canary: SandboxCheck(
            reason="available", executable=Path("/usr/bin/sandbox-exec")
        ),
    )
    serialized = json.dumps(evaluation.report.to_dict(), sort_keys=True)
    assert PRIVATE_TEST_PHRASE not in serialized
    assert PINNED_TEST_COMMIT not in serialized
    assert str(private_voice.voice_root) not in serialized
    for asset in private_voice.asset_paths:
        assert asset.name not in serialized


def test_report_is_private_and_never_overwritten(
    private_voice: PrivateVoiceFixture, tmp_path: Path
) -> None:
    evaluation = evaluate_spike(
        manifest_path=private_voice.manifest_path,
        voice_root=private_voice.voice_root,
        runtime_revision_path=private_voice.runtime_revision_path,
        expected_upstream_commit=PINNED_TEST_COMMIT,
        measurements=passing_measurements(),
        sandbox_probe=lambda _canary: SandboxCheck(
            reason="available", executable=Path("/usr/bin/sandbox-exec")
        ),
    )
    destination = tmp_path / "reports" / "spike.json"
    write_sanitized_report(evaluation.report, destination)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert set(payload) == {
        "schema_version",
        "manifest_status",
        "sandbox_status",
        "performance_status",
        "default_backend",
        "gpt_sovits_enabled",
        "decision_reasons",
        "gate_reasons",
        "metrics",
    }
    with pytest.raises(FileExistsError):
        write_sanitized_report(evaluation.report, destination)


def test_unmeasured_spike_is_avspeech(private_voice: PrivateVoiceFixture) -> None:
    evaluation = evaluate_spike(
        manifest_path=private_voice.manifest_path,
        voice_root=private_voice.voice_root,
        runtime_revision_path=private_voice.runtime_revision_path,
        expected_upstream_commit=PINNED_TEST_COMMIT,
        measurements=None,
        sandbox_probe=lambda _canary: SandboxCheck(
            reason="available", executable=Path("/usr/bin/sandbox-exec")
        ),
    )
    assert evaluation.report.performance_status == "not_run"
    assert evaluation.decision.default_backend == "avspeech"
    assert "spike_not_run" in evaluation.decision.reasons
