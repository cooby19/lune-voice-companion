"""Compose M0.5 validation, sandbox probing, gate evaluation, and fallback choice."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lune.tts_spike.decision import BackendDecision, decide_default_backend
from lune.tts_spike.manifest import PrivateManifestCheck, check_private_manifest
from lune.tts_spike.performance import PerformanceGate, SpikeMeasurements, evaluate_performance
from lune.tts_spike.report import SanitizedSpikeReport, build_sanitized_report
from lune.tts_spike.sandbox import SandboxCheck, probe_sandbox

SandboxProbe = Callable[[Path], SandboxCheck]


@dataclass(frozen=True, slots=True)
class SpikeEvaluation:
    manifest_check: PrivateManifestCheck = field(repr=False)
    sandbox_check: SandboxCheck
    performance_gate: PerformanceGate
    decision: BackendDecision
    report: SanitizedSpikeReport


def _default_sandbox_probe(canary_path: Path) -> SandboxCheck:
    return probe_sandbox(canary_path=canary_path)


def evaluate_spike(
    *,
    manifest_path: Path,
    voice_root: Path,
    runtime_revision_path: Path,
    expected_upstream_commit: str,
    measurements: SpikeMeasurements | None,
    sandbox_probe: SandboxProbe = _default_sandbox_probe,
) -> SpikeEvaluation:
    manifest_check = check_private_manifest(
        manifest_path=manifest_path,
        voice_root=voice_root,
        runtime_revision_path=runtime_revision_path,
        expected_upstream_commit=expected_upstream_commit,
    )
    sandbox_check = (
        sandbox_probe(manifest_path) if manifest_check.ready else SandboxCheck(reason="not_probed")
    )
    performance_gate = evaluate_performance(measurements)
    decision = decide_default_backend(
        manifest_check=manifest_check,
        sandbox_check=sandbox_check,
        performance_gate=performance_gate,
    )
    report = build_sanitized_report(
        manifest_check=manifest_check,
        sandbox_check=sandbox_check,
        performance_gate=performance_gate,
        decision=decision,
    )
    return SpikeEvaluation(
        manifest_check=manifest_check,
        sandbox_check=sandbox_check,
        performance_gate=performance_gate,
        decision=decision,
        report=report,
    )
