"""Choose a release default without ever silently enabling an unsafe GPT worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from lune.tts_spike.manifest import PrivateManifestCheck
from lune.tts_spike.performance import PerformanceGate
from lune.tts_spike.sandbox import SandboxCheck

TTSBackendName = Literal["avspeech", "gpt_sovits"]
DecisionReason = Literal[
    "private_manifest_unavailable",
    "sandbox_unavailable",
    "spike_not_run",
    "performance_gate_failed",
    "gpt_sovits_eligible",
]


@dataclass(frozen=True, slots=True)
class BackendDecision:
    default_backend: TTSBackendName
    gpt_sovits_enabled: bool
    reasons: tuple[DecisionReason, ...]


def decide_default_backend(
    *,
    manifest_check: PrivateManifestCheck,
    sandbox_check: SandboxCheck,
    performance_gate: PerformanceGate,
) -> BackendDecision:
    reasons: list[DecisionReason] = []
    if not manifest_check.ready:
        reasons.append("private_manifest_unavailable")
    if not sandbox_check.available:
        reasons.append("sandbox_unavailable")
    if not performance_gate.evaluated:
        reasons.append("spike_not_run")
    elif not performance_gate.passed:
        reasons.append("performance_gate_failed")

    if reasons:
        return BackendDecision(
            default_backend="avspeech",
            gpt_sovits_enabled=False,
            reasons=tuple(reasons),
        )
    return BackendDecision(
        default_backend="gpt_sovits",
        gpt_sovits_enabled=True,
        reasons=("gpt_sovits_eligible",),
    )
