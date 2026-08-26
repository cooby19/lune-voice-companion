"""Explicitly allowlisted, privacy-safe GPT-SoVITS spike reports."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from lune.tts_spike.decision import BackendDecision, DecisionReason, TTSBackendName
from lune.tts_spike.manifest import ManifestReason, PrivateManifestCheck
from lune.tts_spike.performance import PerformanceGate, PerformanceReason, SanitizedAggregates
from lune.tts_spike.sandbox import SandboxCheck, SandboxReason

PerformanceStatus = Literal["not_run", "passed", "failed"]


@dataclass(frozen=True, slots=True)
class SanitizedSpikeReport:
    manifest_status: ManifestReason
    sandbox_status: SandboxReason
    performance_status: PerformanceStatus
    default_backend: TTSBackendName
    gpt_sovits_enabled: bool
    decision_reasons: tuple[DecisionReason, ...]
    gate_reasons: tuple[PerformanceReason, ...]
    aggregates: SanitizedAggregates | None

    def to_dict(self) -> dict[str, object]:
        metrics: dict[str, object] | None = None
        if self.aggregates is not None:
            metrics = {
                "sample_count": self.aggregates.sample_count,
                "zh_samples": self.aggregates.zh_samples,
                "en_samples": self.aggregates.en_samples,
                "mixed_samples": self.aggregates.mixed_samples,
                "ttfa_p95_ms": self.aggregates.ttfa_p95_ms,
                "rtf_p95": self.aggregates.rtf_p95,
                "peak_rss_bytes": self.aggregates.peak_rss_bytes,
                "worst_cancel_ms": self.aggregates.worst_cancel_ms,
                "worst_thermal_state": self.aggregates.worst_thermal_state,
            }
        return {
            "schema_version": 1,
            "manifest_status": self.manifest_status,
            "sandbox_status": self.sandbox_status,
            "performance_status": self.performance_status,
            "default_backend": self.default_backend,
            "gpt_sovits_enabled": self.gpt_sovits_enabled,
            "decision_reasons": list(self.decision_reasons),
            "gate_reasons": list(self.gate_reasons),
            "metrics": metrics,
        }


def build_sanitized_report(
    *,
    manifest_check: PrivateManifestCheck,
    sandbox_check: SandboxCheck,
    performance_gate: PerformanceGate,
    decision: BackendDecision,
) -> SanitizedSpikeReport:
    if not performance_gate.evaluated:
        performance_status: PerformanceStatus = "not_run"
    elif performance_gate.passed:
        performance_status = "passed"
    else:
        performance_status = "failed"
    return SanitizedSpikeReport(
        manifest_status=manifest_check.reason,
        sandbox_status=sandbox_check.reason,
        performance_status=performance_status,
        default_backend=decision.default_backend,
        gpt_sovits_enabled=decision.gpt_sovits_enabled,
        decision_reasons=decision.reasons,
        gate_reasons=performance_gate.reasons,
        aggregates=performance_gate.aggregates,
    )


def write_sanitized_report(report: SanitizedSpikeReport, destination: Path) -> None:
    """Create, never overwrite, a mode-0600 report containing allowlisted fields only."""

    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    file_descriptor = os.open(destination, flags, 0o600)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(report.to_dict(), handle, ensure_ascii=True, separators=(",", ":"))
            handle.write("\n")
    finally:
        os.close(file_descriptor)
